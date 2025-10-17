import time
import requests
import json
import re
import logging
import io
import os
from typing import List, Dict, Tuple, Optional
from dotenv import load_dotenv, set_key

try:
    import gradio as gr
except ImportError:
    raise ImportError("需要安装 gradio 包：pip install gradio")

# Gemini SDK 可选依赖
try:
    import google.generativeai as genai
except ImportError:
    genai = None

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36 Edg/127.0.0.0"

# .env 持久化设置（将 .env 放在与脚本同目录）
DOTENV_PATH = os.path.join(os.path.dirname(__file__), ".env")
# 读取 .env（如果不存在不会报错）
try:
    load_dotenv(DOTENV_PATH, override=False)
except Exception:
    pass

def load_env_defaults() -> Dict[str, str]:
    return {
        "GEMINI_API_KEY": os.getenv("GEMINI_API_KEY", ""),
        "BILI_SESSDATA": os.getenv("BILI_SESSDATA", ""),
    }

def persist_env(key: str, value: str, logger: logging.Logger) -> None:
    try:
        value = (value or "").strip()
        if value and os.getenv(key) != value:
            set_key(DOTENV_PATH, key, value)
            os.environ[key] = value  # 立即生效
            logger.info(f".env 已更新 {key}")
    except Exception as e:
        logger.warning(f"写入 .env 失败: {e}")


def build_headers(sessdata: str) -> Dict[str, str]:
    headers = {"user-agent": UA}
    if sessdata:
        headers["cookie"] = f"SESSDATA={sessdata}"
    return headers


def extract_bv_from_url(url: str) -> Optional[str]:
    m = re.search(r"(BV[0-9A-Za-z]+)", url)
    return m.group(1) if m else None


def get_aid_cid(url: str, headers: Dict[str, str], logger: logging.Logger) -> Tuple[int, int]:
    response = requests.get(url, headers=headers, timeout=10)
    response.raise_for_status()
    html_content = response.text
    m = re.search(r"window.__INITIAL_STATE__=(.*?);\(function", html_content)
    if not m:
        logger.error("未从页面提取到 INITIAL_STATE，页面结构可能已变更。")
        raise ValueError("无法解析视频页面的 INITIAL_STATE")
    res_data = m.group(1)
    data = json.loads(res_data)
    video_data = data.get("videoData") or {}
    cid = int(video_data.get("cid"))
    aid = int(video_data.get("aid"))
    logger.info(f"解析到 aid={aid}, cid={cid}")
    return aid, cid


def get_subtitle_url(aid: int, cid: int, headers: Dict[str, str], logger: logging.Logger,
                     preferred_langs: Optional[List[str]] = None) -> Optional[str]:
    url = f"https://api.bilibili.com/x/player/wbi/v2?aid={aid}&cid={cid}"
    resp = requests.get(url, headers=headers, timeout=10)
    if resp.status_code != 200:
        logger.error(f"字幕接口 HTTP {resp.status_code}")
        return None
    try:
        j = resp.json()
    except Exception as e:
        logger.error(f"字幕接口返回非 JSON: {e}")
        return None
    if j.get("code") not in (0, None):
        logger.error(f"字幕接口返回错误: code={j.get('code')} message={j.get('message')}")
        # 该接口可能需要 wbi 签名，当前未实现签名
    sub_list = (((j or {}).get("data") or {}).get("subtitle") or {}).get("subtitles") or []
    if not sub_list:
        logger.warning("该视频无可用字幕。")
        return None
    preferred_langs = preferred_langs or ["zh-CN", "zh-Hans", "zh-Hant", "zh", "en"]
    chosen = None
    for lang in preferred_langs:
        for item in sub_list:
            if item.get("lan") == lang:
                chosen = item
                break
        if chosen:
            break
    chosen = chosen or sub_list[0]
    subtitle_url = chosen.get("subtitle_url", "")
    if subtitle_url.startswith("//"):
        subtitle_url = "https:" + subtitle_url
    logger.info(f"选择字幕: lan={chosen.get('lan')} lan_doc={chosen.get('lan_doc')} url={subtitle_url}")
    return subtitle_url


def fetch_subtitle_body(subtitle_url: str, headers: Dict[str, str], logger: logging.Logger) -> List[Dict]:
    response = requests.get(subtitle_url, headers=headers, timeout=10)
    response.raise_for_status()
    j = response.json()
    body = j.get("body") or []
    logger.info(f"获取到字幕条数: {len(body)}")
    return body


def secs_to_hms_ms(s: float) -> str:
    ms = int((s - int(s)) * 1000)
    total = int(s)
    h = total // 3600
    m = (total % 3600) // 60
    sec = total % 60
    return f"{h:02d}:{m:02d}:{sec:02d}.{ms:03d}"


def build_subtitle_text(body: List[Dict], include_time: bool = True) -> str:
    lines = []
    for i in body:
        content = i.get("content", "")
        if include_time:
            time_from = secs_to_hms_ms(float(i.get("from", 0)))
            time_to = secs_to_hms_ms(float(i.get("to", 0)))
            lines.append(f"{time_from} -> {time_to} | {content}")
        else:
            lines.append(content)
    return "\n".join(lines)


def save_text_to_file(base_name: str, text: str) -> str:
    base_dir = os.path.dirname(__file__)
    file_name = f"subtitle_{base_name}.txt"
    path = os.path.join(base_dir, file_name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def _chunk_text(text: str, max_chars: int = 12000) -> List[str]:
    chunks = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + max_chars, n)
        # 尽量在换行处截断，提升语义完整性
        cut = text.rfind("\n", start, end)
        if cut == -1 or cut <= start:
            cut = end
        chunks.append(text[start:cut])
        start = cut
    return chunks


def summarize_with_gemini(text: str, api_key: str, logger: logging.Logger) -> str:
    if not api_key:
        logger.warning("未提供 Gemini API Key，跳过总结。")
        return ""
    if genai is None:
        logger.error("未安装 google-generativeai，请先安装：pip install -U google-generativeai")
        return ""
    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel("gemini-2.5-flash")

        chunks = _chunk_text(text, max_chars=12000)
        partials = []
        for idx, ck in enumerate(chunks, 1):
            logger.info(f"Gemini 分段总结 {idx}/{len(chunks)}…")
            prompt = (
                "你是一名高质量的内容总结助手。"
                "请对以下字幕文本进行结构化总结，包含：主题、关键要点、时间线要点、结论与行动项。"
                "尽量保留视频中的关键信息与术语，使用简体中文，分点列出。\n\n"
                f"字幕片段 {idx}：\n{ck}\n"
            )
            resp = model.generate_content(prompt)
            partials.append(getattr(resp, "text", str(resp)))

        if len(partials) == 1:
            return partials[0]

        logger.info("Gemini 汇总合并多个分段总结…")
        merge_prompt = (
            "下面是多个分段总结，请综合为一份完整总结，要求："
            "1) 保留关键信息与因果；2) 去重与合并相近要点；3) 给出结构化大纲与结论；"
            "4) 用简体中文。\n\n"
            + "\n\n".join(f"分段{idx}：\n{p}" for idx, p in enumerate(partials, 1))
        )
        final_resp = model.generate_content(merge_prompt)
        return getattr(final_resp, "text", str(final_resp))
    except Exception as e:
        logger.exception(f"Gemini 总结失败: {e}")
        return ""


def process(video_url: str, sessdata: str, api_key: str) -> Tuple[str, str, str]:
    logger = logging.getLogger("bili_subtitle")
    logger.setLevel(logging.INFO)
    log_buffer = io.StringIO()
    handler = logging.StreamHandler(log_buffer)
    handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    # 清理旧的 handler，避免重复日志
    for h in list(logger.handlers):
        logger.removeHandler(h)
    logger.addHandler(handler)

    try:
        # 优先使用用户输入，其次回退到 .env
        defaults = load_env_defaults()
        entered_sess = (sessdata or "").strip()
        entered_key = (api_key or "").strip()
        sess_to_use = entered_sess or defaults.get("BILI_SESSDATA", "")
        key_to_use = entered_key or defaults.get("GEMINI_API_KEY", "")

        headers = build_headers(sess_to_use)
        bv = extract_bv_from_url(video_url) or "aid_cid"

        logger.info("开始解析视频地址…")
        aid, cid = get_aid_cid(video_url, headers, logger)
        time.sleep(0.5)

        logger.info("获取字幕地址…")
        subtitle_url = get_subtitle_url(aid, cid, headers, logger)
        if not subtitle_url:
            raise RuntimeError("未获取到字幕地址")
        time.sleep(0.5)

        logger.info("拉取字幕数据…")
        body = fetch_subtitle_body(subtitle_url, headers, logger)
        if not body:
            raise RuntimeError("字幕数据为空")

        text = build_subtitle_text(body, include_time=True)
        fname_key = bv if bv != "aid_cid" else f"aid{aid}_cid{cid}"
        path = save_text_to_file(fname_key, text)
        logger.info(f"字幕已保存至: {path}")

        logger.info("开始使用 Gemini 生成视频总结…")
        summary = summarize_with_gemini(text, key_to_use, logger)

        # 将用户手动输入的敏感信息持久化到 .env（仅当非空且与现有不同）
        if entered_sess:
            persist_env("BILI_SESSDATA", entered_sess, logger)
        if entered_key:
            persist_env("GEMINI_API_KEY", entered_key, logger)

        return text, summary, log_buffer.getvalue()
    except Exception as e:
        logger.exception(f"处理失败: {e}")
        return "", "", log_buffer.getvalue()
    finally:
        logger.removeHandler(handler)


def launch_gradio():
    defaults = load_env_defaults()
    with gr.Blocks(title="Bilibili 字幕提取 + Gemini 总结") as demo:
        gr.Markdown("## B站字幕提取与总结\n输入视频地址、SESSDATA 与 Gemini API Key，提取完整字幕，调用 Gemini 2.5 Flash 总结视频内容。")
        with gr.Row():
            video_url = gr.Textbox(label="视频地址", placeholder="https://www.bilibili.com/video/BV...", lines=1)
            sessdata = gr.Textbox(label="SESSDATA", placeholder="你的SESSDATA", type="password", lines=1, value=defaults.get("BILI_SESSDATA", ""))
            api_key = gr.Textbox(label="Gemini API Key", placeholder="填写你的 Google AI Studio API Key", type="password", lines=1, value=defaults.get("GEMINI_API_KEY", ""))
        run_btn = gr.Button("获取字幕并总结")
        with gr.Row():
            subtitle_out = gr.Textbox(label="查看字幕", lines=18)
            summary_out = gr.Textbox(label="Gemini 总结", lines=18)
            log_out = gr.Textbox(label="终端日志", lines=18)
        run_btn.click(fn=process, inputs=[video_url, sessdata, api_key], outputs=[subtitle_out, summary_out, log_out])
    demo.launch()


if __name__ == "__main__":
    launch_gradio()