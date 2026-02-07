import time
import re
from urllib.parse import urlparse, urlunparse

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

USER_AGENT = "Mozilla/5.0 (compatible; ProductPageScanner/1.7)"

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
# Platform detection (감지용 패턴 확장)
# ----------------------------
def detect_platform_from_product_url(product_url: str):
    """
    감지용 패턴:
    - Cafe24:
        1) /surl/p/{id}
        2) /product/.../{id}/category/...
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

    # ---- Cafe24 (B): /product/.../{id}/category/...
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
# Scanner
# ----------------------------
def scan(template_url: str):
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    product_id = 1
    consecutive_misses = 0
    extra_retry_used = False

    found_products = []
    found_urls = set()

    while True:
        url = template_url.format(id=product_id)
        print(f"[CHECK] {url}")

        try:
            r = session.get(url, allow_redirects=True, timeout=TIMEOUT_SEC)

            if looks_not_found(r.status_code, url, r.url, r.text or ""):
                consecutive_misses += 1
                print(f"  -> NOT FOUND ({consecutive_misses})")
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
            print(f"  -> ERROR: {e} ({consecutive_misses})")

        # 종료 조건 (초반 30번 전부 실패 시 1회 추가 허용)
        if consecutive_misses >= STOP_AFTER_CONSECUTIVE_MISSES:
            if not found_products and not extra_retry_used:
                print("\n[INFO] 아직 제품을 찾지 못해 추가 30회 스캔을 진행합니다.\n")
                consecutive_misses = 0
                extra_retry_used = True
            else:
                break

        product_id += 1
        time.sleep(SLEEP_SEC)

    return found_products

# ----------------------------
# Main
# ----------------------------
def main():
    print("제품 페이지 URL을 입력하세요 (UTM 포함 가능)")
    print("예) https://brainology.kr/surl/p/10")
    print("예) https://brainology.kr/product/.../10/category/24/display/1/")
    print("예) https://www.realcumin.kr/Product/?idx=72")
    product_url = input("> ").strip()

    platform, template_url = detect_platform_from_product_url(product_url)

    if not platform:
        print("\n[ERROR] 처음 보는 페이지 패턴입니다.")
        print(f"입력한 주소: {product_url}")
        return

    print(f"\n[INFO] 플랫폼: {platform}")
    print(f"[INFO] 실제 스캔 URL 패턴: {template_url}")
    print("\n[START]\n")

    results = scan(template_url)

    print("\n" + "=" * 50)
    print("📦 스캔 결과 요약 (제품명 + URL)")
    print("=" * 50)

    if not results:
        print("찾은 제품 페이지가 없습니다.")
        return

    for idx, (name, url) in enumerate(results, 1):
        print(f"{idx}. {name}")
        print(f"   {url}")

    print("\n총 발견 제품 수:", len(results))


if __name__ == "__main__":
    main()
