import time
import re
from urllib.parse import urlparse, urlunparse, parse_qs

import requests

# ----------------------------
# Settings
# ----------------------------
NOT_FOUND_KEYWORDS = [
    "페이지를 찾을 수", "찾을 수 없습니다", "존재하지",
    "삭제된", "판매중지", "상품이 없습니다",
    "없는 상품", "not found", "404"
]

SLEEP_SEC = 1.0
STOP_AFTER_CONSECUTIVE_MISSES = 100
TIMEOUT_SEC = 10

USER_AGENT = "Mozilla/5.0 (compatible; ProductPageScanner/1.9)"

# ----------------------------
# URL utils
# ----------------------------
def ensure_scheme(url: str) -> str:
    url = url.strip()
    if not re.match(r"^https?://", url, re.IGNORECASE):
        return "https://" + url
    return url

def normalize_home(url: str) -> str:
    u = ensure_scheme(url)
    p = urlparse(u)
    return urlunparse((p.scheme, p.netloc, "", "", "", ""))

def strip_query_fragment(url: str) -> str:
    u = ensure_scheme(url)
    p = urlparse(u)
    return urlunparse((p.scheme, p.netloc, p.path, "", "", ""))

def is_homepage(url: str) -> bool:
    p = urlparse(url)
    return (p.path or "").rstrip("/") in ["", "/"]

# ----------------------------
# Product ID extraction
# ----------------------------
def extract_product_id_from_input_url(product_url: str) -> int | None:
    """
    Extract product id from supported input URL patterns.
    - Cafe24 A: /surl/p/{id}
    - Cafe24 B: /product/.../{id}/category/...
      (id is the number right before '/category/')
    - Imweb: /Product/?idx={id}
    """
    raw = ensure_scheme(product_url)
    clean = strip_query_fragment(raw)

    p_clean = urlparse(clean)
    p_raw = urlparse(raw)

    path = p_clean.path or ""
    query = p_raw.query or ""

    # Cafe24 A
    m = re.search(r"/surl/p/(\d+)", path)
    if m:
        return int(m.group(1))

    # Cafe24 B (id right before /category/)
    m = re.search(r"/product/.+/(\d+)/category/", path)
    if m:
        return int(m.group(1))

    # Imweb idx
    if path.rstrip("/").lower().endswith("/product"):
        qs = parse_qs(query)
        if "idx" in qs and qs["idx"]:
            v = qs["idx"][0]
            if re.match(r"^\d+$", v):
                return int(v)

    return None

# ----------------------------
# Platform detection (감지용 패턴 확장) + 스캔 템플릿 확정
# ----------------------------
def detect_platform_from_product_url(product_url: str):
    """
    감지용 패턴:
    - Cafe24:
        1) /surl/p/{id}
        2) /product/.../{id}/category/... (감지 전용)
       → 감지 후 스캔은 항상 /surl/p/{id}
    - Imweb:
        /Product/?idx={id}
    """
    raw = ensure_scheme(product_url)
    clean = strip_query_fragment(raw)

    parsed_clean = urlparse(clean)
    parsed_raw = urlparse(raw)

    path = parsed_clean.path or ""
    query = parsed_raw.query or ""
    base = normalize_home(clean)

    # ---- Cafe24 (A): /surl/p/{id}
    if "/surl/p/" in path and re.search(r"/surl/p/\d+", path):
        return "cafe24", f"{base}/surl/p/{{id}}"

    # ---- Cafe24 (B): /product/.../{id}/category/...  (감지 전용)
    if path.startswith("/product/") and re.search(r"/product/.+/\d+/category/", path):
        return "cafe24", f"{base}/surl/p/{{id}}"

    # ---- Imweb: /Product/?idx={id}
    if path.rstrip("/").lower().endswith("/product"):
        if re.search(r"(?:^|&)idx=\d+(?:&|$)", query, re.IGNORECASE):
            return "imweb", f"{base}/Product/?idx={{id}}"

    return None, None

# ----------------------------
# Product name parsing
# ----------------------------
def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()

def extract_product_name(html: str) -> str:
    patterns = [
        r'<meta[^>]+property=["\']og:title["\'][^>]*content=["\']([^"\']+)["\']',
        r'<meta[^>]+name=["\']twitter:title["\'][^>]*content=["\']([^"\']+)["\']',
        r"<title[^>]*>(.*?)</title>",
    ]
    for pat in patterns:
        m = re.search(pat, html, re.IGNORECASE | re.DOTALL)
        if m:
            return clean_text(m.group(1))
    return "(제품명 추출 실패)"

# ----------------------------
# Not-found 판단
# ----------------------------
def looks_not_found(status_code: int, requested_url: str, final_url: str, html: str) -> bool:
    if status_code != 200:
        return True

    # 없는 상품이면 홈으로 리다이렉트되는 케이스
    if requested_url.rstrip("/") != final_url.rstrip("/") and is_homepage(final_url):
        return True

    sample = (html[:20000] or "").lower()
    for kw in NOT_FOUND_KEYWORDS:
        if kw in sample:
            return True

    if len(sample.strip()) < 200:
        return True

    return False

# ----------------------------
# Scanner (1-pass)
# ----------------------------
def scan_pass(
    template_url: str,
    start_id: int,
    stop_after_consecutive_misses: int,
    sleep_sec: float,
    allow_extra_retry_if_zero_found: bool,
    found_products: list[tuple[str, str]] | None = None,
    found_urls: set[str] | None = None,
):
    """
    One scanning pass.
    - starts from start_id
    - stops when consecutive misses reach stop_after_consecutive_misses
    - optional extra retry ONLY when allow_extra_retry_if_zero_found=True
      and found_products is still empty at first stop trigger.
    """
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    product_id = start_id
    consecutive_misses = 0
    extra_retry_used = False

    if found_products is None:
        found_products = []
    if found_urls is None:
        found_urls = set()

    while True:
        url = template_url.format(id=product_id)
        print(f"[CHECK] {url}")

        try:
            r = session.get(url, allow_redirects=True, timeout=TIMEOUT_SEC)

            if looks_not_found(r.status_code, url, r.url, r.text or ""):
                consecutive_misses += 1
                print(f"  -> NOT FOUND ({consecutive_misses}/{stop_after_consecutive_misses})")
            else:
                consecutive_misses = 0
                final_url = r.url

                if final_url not in found_urls:
                    name = extract_product_name(r.text or "")
                    found_products.append((name, final_url))
                    found_urls.add(final_url)

                print(f"  ✅ FOUND: {final_url}")

        except requests.RequestException as e:
            consecutive_misses += 1
            print(f"  -> ERROR: {e} ({consecutive_misses}/{stop_after_consecutive_misses})")

        if consecutive_misses >= stop_after_consecutive_misses:
            if allow_extra_retry_if_zero_found and (len(found_products) == 0) and (not extra_retry_used):
                print(f"\n[INFO] 아직 제품을 하나도 찾지 못해 추가 {stop_after_consecutive_misses}회 스캔을 진행합니다.\n")
                consecutive_misses = 0
                extra_retry_used = True
            else:
                break

        product_id += 1
        time.sleep(sleep_sec)

    return found_products, found_urls

# ----------------------------
# Main
# ----------------------------
def main():
    print("제품 페이지 URL을 입력하세요 (UTM 포함 가능)")
    print("예) https://brainology.kr/surl/p/10")
    print("예) https://brainology.kr/product/.../10/category/24/display/1/  (감지 전용, 스캔은 /surl/p/{id})")
    print("예) https://www.realcumin.kr/Product/?idx=72")
    product_url = input("> ").strip()

    platform, template_url = detect_platform_from_product_url(product_url)
    if not platform:
        print("\n[ERROR] 처음 보는 페이지 패턴입니다.")
        print(f"입력한 주소: {product_url}")
        return

    input_product_id = extract_product_id_from_input_url(product_url)
    if input_product_id is None:
        print("\n[ERROR] 입력 URL에서 제품 id를 추출하지 못했습니다.")
        print(f"입력한 주소: {product_url}")
        return

    print(f"\n[INFO] 플랫폼: {platform}")
    print(f"[INFO] 실제 스캔 URL 패턴: {template_url}")
    print(f"[INFO] 입력 URL 제품 id: {input_product_id}")
    print(f"[INFO] 중단 기준: 연속 {STOP_AFTER_CONSECUTIVE_MISSES}회 NOT FOUND/ERROR")
    print(f"[INFO] 스캔 속도: {SLEEP_SEC}초에 1회")
    print("\n[START] 1차 스캔 (start=1)\n")

    # 1) First pass: start at 1, with "extra retry" if zero found
    found_products, found_urls = scan_pass(
        template_url=template_url,
        start_id=1,
        stop_after_consecutive_misses=STOP_AFTER_CONSECUTIVE_MISSES,
        sleep_sec=SLEEP_SEC,
        allow_extra_retry_if_zero_found=True,
        found_products=[],
        found_urls=set(),
    )

    # 2) Conditional second pass
    threshold = input_product_id * 0.01  # as requested
    if len(found_products) < threshold:
        print("\n" + "-" * 60)
        print("[INFO] 추가 조건 트리거!")
        print(f"[INFO] 1차 발견 개수({len(found_products)}) < 입력 제품 id * 0.01 ({threshold:.2f})")
        print(f"[INFO] 2차 스캔을 입력 제품 id({input_product_id})부터 시작합니다.")
        print("-" * 60 + "\n")

        found_products, found_urls = scan_pass(
            template_url=template_url,
            start_id=input_product_id,
            stop_after_consecutive_misses=STOP_AFTER_CONSECUTIVE_MISSES,
            sleep_sec=SLEEP_SEC,
            allow_extra_retry_if_zero_found=False,  # 요구사항대로: 연속 100번 안 나올 때까지
            found_products=found_products,
            found_urls=found_urls,
        )
    else:
        print("\n[INFO] 추가 2차 스캔 조건 미충족 (추가 스캔 없음)")

    # Final summary
    print("\n" + "=" * 50)
    print("📦 스캔 결과 요약 (제품명 + URL)")
    print("=" * 50)

    if not found_products:
        print("찾은 제품 페이지가 없습니다.")
        return

    for idx, (name, url) in enumerate(found_products, 1):
        print(f"{idx}. {name}")
        print(f"   {url}")

    print("\n총 발견 제품 수:", len(found_products))

def scan_for_slack(product_url: str):
    """
    Slack bot용 엔트리 함수
    - Slack에서는 product_url 하나만 넘기면 됨
    - 내부 로직은 CLI와 동일
    """
    platform, template_url = detect_platform_from_product_url(product_url)
    if not platform:
        raise ValueError("Unsupported product URL pattern")

    input_product_id = extract_product_id_from_input_url(product_url)
    if input_product_id is None:
        raise ValueError("Failed to extract product id from URL")

    # 1차 스캔
    found_products, found_urls = scan_pass(
        template_url=template_url,
        start_id=1,
        stop_after_consecutive_misses=STOP_AFTER_CONSECUTIVE_MISSES,
        sleep_sec=SLEEP_SEC,
        allow_extra_retry_if_zero_found=True,
        found_products=[],
        found_urls=set(),
    )

    # 조건부 2차 스캔
    if len(found_products) < (input_product_id * 0.01):
        found_products, found_urls = scan_pass(
            template_url=template_url,
            start_id=input_product_id,
            stop_after_consecutive_misses=STOP_AFTER_CONSECUTIVE_MISSES,
            sleep_sec=SLEEP_SEC,
            allow_extra_retry_if_zero_found=False,
            found_products=found_products,
            found_urls=found_urls,
        )

    return found_products

if __name__ == "__main__":
    main()
