import { Context, Schema } from 'koishi'
import fs from 'node:fs/promises'
import path from 'node:path'

export const name = 'bilibili-summary'
export const inject = { required: ['http'] }
export const usage = '输入“分析 [B站视频地址]”提取字幕并用 Gemini 总结。'

export const Config = Schema.object({
  sessdata: Schema.string().role('secret').description('B站 SESSDATA Cookie，用于提升字幕接口成功率').default(''),
  geminiKey: Schema.string().role('secret').description('Google Gemini API Key').default(''),
  preferredLangs: Schema.array(Schema.string()).description('字幕语言优先级').default(['zh-CN', 'zh-Hans', 'zh-Hant', 'zh', 'en']),
  includeTime: Schema.boolean().description('显示字幕时包含时间戳').default(true),
  saveSubtitle: Schema.boolean().description('保存字幕到本地文件').default(false),
}).description('提取 B 站视频字幕并用 Gemini 总结')

const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36 Edg/127.0.0.0'

export function apply(ctx: Context, config) {
  const logger = ctx.logger('bilibili-summary')
  const preferred = config.preferredLangs?.length ? config.preferredLangs : ['zh-CN', 'zh-Hans', 'zh-Hant', 'zh', 'en']

  ctx.command('分析 <url:text>', '提取 B 站字幕并用 Gemini 总结')
    .alias('bili分析')
    .example('分析 https://www.bilibili.com/video/BV...')
    .action(async ({ session }, url) => {
      if (!url) return '请提供 B 站视频地址。'
      const headers = buildHeaders(config.sessdata || '')
      let aid, cid, bv, body, text, summary, subtitleUrl
      try {
        await session.send('开始解析视频地址…')
        const r = await getAidCid(ctx, url, headers)
        aid = r.aid; cid = r.cid
        bv = extractBV(url) || `aid${aid}_cid${cid}`

        await session.send('获取字幕地址…')
        subtitleUrl = await getSubtitleUrl(ctx, aid, cid, headers, preferred)
        if (!subtitleUrl) return '未获取到字幕地址，可能需要有效的 SESSDATA 或接口发生变化。'

        await session.send('拉取字幕数据…')
        body = await fetchSubtitleBody(ctx, subtitleUrl, headers)
        if (!body?.length) return '字幕数据为空。'

        text = buildSubtitleText(body, !!config.includeTime)
        if (config.saveSubtitle) {
          try {
            const p = await saveTextToFile(bv, text)
            logger.info(`字幕已保存: ${p}`)
          } catch (e) {
            logger.warn(`保存字幕失败: ${e?.message || e}`)
          }
        }

        await session.send('开始使用 Gemini 生成总结…')
        summary = await summarizeWithGemini(text, config.geminiKey, logger)

        const result = summary?.trim()
          ? `Gemini 总结：\n${summary}`
          : 'Gemini 总结为空（未配置 API Key 或调用失败）。'

        // 如果字幕也想回显，可取消下行注释：
        // await session.send(h.quote(session.messageId) + '字幕：\n' + text.slice(0, 4000))

        return result
      } catch (e) {
        logger.error(e)
        return `处理失败：${e?.message || e}`
      }
    })
}

function buildHeaders(sessdata) {
  const headers = { 'user-agent': UA }
  if (sessdata) headers.cookie = `SESSDATA=${sessdata}`
  return headers
}

function extractBV(url) {
  const m = (url || '').match(/(BV[0-9A-Za-z]+)/)
  return m ? m[1] : null
}

function secsToHmsMs(s) {
  const ms = Math.floor((s - Math.floor(s)) * 1000)
  const total = Math.floor(s)
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const sec = total % 60
  const pad = (n, w = 2) => String(n).padStart(w, '0')
  return `${pad(h)}:${pad(m)}:${pad(sec)}.${pad(ms, 3)}`
}

function buildSubtitleText(body, includeTime = true) {
  const lines = []
  for (const i of body || []) {
    const content = i?.content ?? ''
    if (includeTime) {
      const t1 = secsToHmsMs(Number(i?.from || 0))
      const t2 = secsToHmsMs(Number(i?.to || 0))
      lines.push(`${t1} -> ${t2} | ${content}`)
    } else {
      lines.push(String(content))
    }
  }
  return lines.join('\n')
}

async function getAidCid(ctx, url, headers) {
  const html = await ctx.http.get(url, { headers, timeout: 10000 })
  const m = html.match(/window\.__INITIAL_STATE__=(.*?);\(function/s)
  if (!m) throw new Error('未从页面提取到 INITIAL_STATE，页面结构可能已变更')
  const json = JSON.parse(m[1])
  const videoData = json.videoData || {}
  const cid = Number(videoData.cid)
  const aid = Number(videoData.aid)
  if (!aid || !cid) throw new Error('未能解析 aid/cid')
  return { aid, cid }
}

async function getSubtitleUrl(ctx, aid, cid, headers, preferredLangs) {
  const url = `https://api.bilibili.com/x/player/wbi/v2?aid=${aid}&cid=${cid}`
  const j = await ctx.http.get(url, { headers, timeout: 10000 }).then(r => {
    try { return JSON.parse(r) } catch { return r }
  }).catch(() => null)
  if (!j || (j.code && j.code !== 0)) {
    // 接口可能需要 wbi 签名，尝试备用接口 (不保证可用)
    // 备用：x/player/v2
    try {
      const alt = await ctx.http.get(`https://api.bilibili.com/x/player/v2?aid=${aid}&cid=${cid}`, { headers, timeout: 10000 })
      const jj = typeof alt === 'string' ? JSON.parse(alt) : alt
      const subList = jj?.data?.subtitle?.subtitles || []
      return pickSubtitleUrl(subList, preferredLangs)
    } catch (e) {
      return null
    }
  }
  const subList = j?.data?.subtitle?.subtitles || []
  return pickSubtitleUrl(subList, preferredLangs)
}

function pickSubtitleUrl(subList, preferredLangs) {
  if (!subList?.length) return null
  let chosen = null
  for (const lang of preferredLangs || ['zh-CN', 'zh-Hans', 'zh-Hant', 'zh', 'en']) {
    chosen = subList.find(i => i?.lan === lang)
    if (chosen) break
  }
  if (!chosen) chosen = subList[0]
  let surl = chosen?.subtitle_url || ''
  if (surl.startsWith('//')) surl = 'https:' + surl
  return surl
}

async function fetchSubtitleBody(ctx, subtitleUrl, headers) {
  const j = await ctx.http.get(subtitleUrl, { headers, timeout: 10000 }).then(r => {
    try { return JSON.parse(r) } catch { return r }
  })
  return j?.body || []
}

function chunkText(text, max = 12000) {
  const chunks = []
  let start = 0
  while (start < text.length) {
    const end = Math.min(start + max, text.length)
    let cut = text.lastIndexOf('\n', end)
    if (cut < start) cut = end
    chunks.push(text.slice(start, cut))
    start = cut
  }
  return chunks
}

async function summarizeWithGemini(text, apiKey, logger) {
  if (!apiKey) {
    logger?.info('未提供 Gemini API Key，跳过总结')
    return ''
  }
  let gen
  try {
    const mod = await import('@google/generative-ai')
    const GoogleGenerativeAI = mod.GoogleGenerativeAI || mod.default?.GoogleGenerativeAI
    gen = new GoogleGenerativeAI(apiKey)
  } catch (e) {
    logger?.error('未安装 @google/generative-ai，请先安装：npm i @google/generative-ai')
    return ''
  }
  try {
    const model = gen.getGenerativeModel({ model: 'gemini-2.0-flash' })
    const chunks = chunkText(text, 12000)
    const partials = []
    for (let i = 0; i < chunks.length; i++) {
      const ck = chunks[i]
      logger?.info(`Gemini 分段总结 ${i + 1}/${chunks.length}…`)
      const prompt = [
        '你是一名高质量的内容总结助手。',
        '请对以下字幕文本进行结构化总结，包含：主题、关键要点、时间线要点、结论与行动项。',
        '尽量保留视频中的关键信息与术语，使用简体中文，分点列出。',
        `字幕片段 ${i + 1}：\n${ck}\n`,
      ].join('\n')
      const resp = await model.generateContent(prompt)
      partials.push(resp?.response?.text() ?? '')
    }
    if (partials.length <= 1) return partials[0] || ''
    logger?.info('Gemini 汇总合并多个分段总结…')
    const mergePrompt = [
      '下面是多个分段总结，请综合为一份完整总结，要求：',
      '1) 保留关键信息与因果；2) 去重与合并相近要点；3) 给出结构化大纲与结论；',
      '4) 用简体中文。',
      partials.map((p, idx) => `分段${idx + 1}：\n${p}`).join('\n\n'),
    ].join('\n\n')
    const finalResp = await model.generateContent(mergePrompt)
    return finalResp?.response?.text() ?? ''
  } catch (e) {
    logger?.error(`Gemini 总结失败: ${e?.message || e}`)
    return ''
  }
}

async function saveTextToFile(baseName, text) {
  const safe = baseName.replace(/[^\w.-]+/g, '_')
  const file = `subtitle_${safe}.txt`
  const base = process.cwd()
  const full = path.join(base, file)
  await fs.writeFile(full, text, 'utf8')
  return full
}
