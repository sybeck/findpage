# findpage/slack_bot.py
import os
import re
import threading
from pathlib import Path

from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from find_page import scan_for_slack, detect_platform_from_product_url

load_dotenv(Path(__file__).with_name(".env"))

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_APP_TOKEN = os.getenv("SLACK_APP_TOKEN")
TARGET_CHANNEL_ID = os.getenv("SLACK_CHANNEL_ID")  # 비워두면 모든 채널

if not SLACK_BOT_TOKEN or not SLACK_APP_TOKEN:
    raise RuntimeError("SLACK_BOT_TOKEN / SLACK_APP_TOKEN 을 .env에 설정하세요.")

app = App(token=SLACK_BOT_TOKEN)

URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)


def extract_first_url(text: str) -> str | None:
    if not text:
        return None
    m = URL_RE.search(text)
    return m.group(0) if m else None


def format_results(results: list[tuple[str, str]], new_count: int = 0) -> str:
    if not results:
        return "찾은 제품 페이지가 없습니다."
    lines = []
    total = len(results)
    existing = total - new_count
    
    lines.append(f"📊 전체 제품 수: {total}개 (기존: {existing}개, 신규: {new_count}개)\n")
    
    for i, (name, url) in enumerate(results, 1):
        lines.append(f"{i}. {name}\n{url}")
    return "\n\n".join(lines)


def post_thread(client, channel: str, thread_ts: str, text: str):
    # ✅ 항상 스레드에만 답글
    client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text)


def upload_file_to_thread(client, channel: str, thread_ts: str, file_path: str, title: str):
    """
    파일을 스레드에 업로드 (실패 시 텍스트로 전달)
    """
    try:
        # 먼저 파일 업로드 시도
        with open(file_path, 'rb') as file_content:
            client.files_upload_v2(
                channel=channel,
                thread_ts=thread_ts,
                file=file_content,
                title=title,
                filename=os.path.basename(file_path)
            )
        print(f"[INFO] 파일 업로드 완료: {file_path}")
    except Exception as e:
        print(f"[ERROR] 파일 업로드 실패: {e}")
        # 파일 업로드 실패 시 내용을 텍스트로 전달
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
            # 메시지 길이 제한 (3000자)
            if len(content) > 3000:
                content = content[:3000] + "\n...(생략)"
            post_thread(
                client,
                channel,
                thread_ts,
                f"📄 **{title}**\n```\n{content}\n```"
            )
            print(f"[INFO] 파일 내용을 텍스트로 전달: {file_path}")
        except Exception as text_error:
            print(f"[ERROR] 텍스트 전달도 실패: {text_error}")
            post_thread(client, channel, thread_ts, f"❌ 파일 전달 실패: {e}")


def run_scan_and_reply(client, channel: str, thread_ts: str, product_url: str):
    try:
        platform, template_url = detect_platform_from_product_url(product_url)
        if not platform:
            post_thread(
                client,
                channel,
                thread_ts,
                f"❌ 처음 보는 페이지 패턴입니다.\n입력한 주소: {product_url}",
            )
            return

        # ✅ 스캔 시작 전에 플랫폼/패턴 포함해서 안내
        post_thread(
            client,
            channel,
            thread_ts,
            "🔎 스캔 시작\n"
            f"- 감지 플랫폼: {platform}\n"
            f"- 스캔 패턴: {template_url}\n"
            f"- 속도: 1초 1회\n"
            f"- 중단: 연속 100회 NOT FOUND/ERROR\n"
            f"- 추가: 초반 0건이면 100회 1회 추가, "
            f"또는 (발견 수 < 입력 제품ID*0.01)면 입력 제품ID부터 재스캔",
        )

        # scan_for_slack는 이제 (전체 제품, 신규 제품, 인플루언서 파일명) 반환
        all_products, new_products, influencer_file = scan_for_slack(product_url)

        # 결과 메시지 (전체 제품 + 신규 개수 표시)
        post_thread(
            client,
            channel,
            thread_ts,
            "✅ 스캔 결과\n\n" + format_results(all_products, len(new_products)),
        )

        # 인플루언서 파일이 생성되었으면 업로드
        if influencer_file and os.path.exists(influencer_file):
            upload_file_to_thread(
                client,
                channel,
                thread_ts,
                influencer_file,
                "인플루언서명 추출 결과"
            )

    except Exception as e:
        post_thread(
            client,
            channel,
            thread_ts,
            f"❌ 스캔 중 오류 발생\n{type(e).__name__}: {e}",
        )


@app.event("message")
def handle_message_events(body, event, client, logger):
    if event.get("subtype"):
        return

    channel = event.get("channel")
    if TARGET_CHANNEL_ID and channel != TARGET_CHANNEL_ID:
        return

    text = event.get("text", "")
    ts = event.get("ts")  # 원문 메시지 ts = 스레드 루트

    url = extract_first_url(text)
    if not url:
        return

    # ✅ 채널에 새 메시지 만들지 않고 스레드에만
    post_thread(client, channel, ts, f"URL을 감지했습니다. 확인해 보겠습니다!")

    t = threading.Thread(
        target=run_scan_and_reply,
        args=(client, channel, ts, url),
        daemon=True,
    )
    t.start()


if __name__ == "__main__":
    SocketModeHandler(app, SLACK_APP_TOKEN).start()
