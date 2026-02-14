import time
import re
import os
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

# ✅ 비정상 상황 감지: 연속으로 "FOUND"가 너무 오래 지속되는 경우
STOP_AFTER_CONSECUTIVE_HITS = 200

USER_AGENT = "Mozilla/5.0 (compatible; ProductPageScanner/2.2)"

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

def get_domain_from_url(url: str) -> str:
    """
    URL에서 도메인명을 추출 (예: 'brainology.kr')
    """
    u = ensure_scheme(url)
    p = urlparse(u)
    return p.netloc.replace('www.', '')

def strip_query_fragment(url: str) -> str:
    """
    Remove ?query and #fragment for stable path detection.
    """
    u = ensure_scheme(url)
    p = urlparse(u)
    return urlunparse((p.scheme, p.netloc, p.path, "", "", ""))

def is_homepage(url: str) -> bool:
    """
    ✅ '없는 상품 → 홈/인덱스로 리다이렉트'를 잡기 위해 홈 판별을 넓게.
    """
    p = urlparse(ensure_scheme(url))
    path = (p.path or "").lower().strip()

    # "/" 또는 "" (기본 홈)
    if path in ["", "/"]:
        return True

    # 흔한 홈/인덱스 경로
    home_like_paths = {
        "/index.html",
        "/index.htm",
        "/index.php",
        "/index.asp",
        "/index.aspx",
        "/default.asp",
        "/default.aspx",
        "/main",
        "/main/",
        "/main/index.html",
        "/main/index.htm",
        "/main/index.php",
    }
    if path in home_like_paths:
        return True

    return False

def normalize_for_compare(url: str) -> str:
    """
    URL 비교용 정규화 (쿼리/프래그먼트 제거 + 호스트/스킴 소문자 + trailing slash 제거)
    """
    p = urlparse(ensure_scheme(url))
    path = (p.path or "").rstrip("/")
    return f"{p.scheme.lower()}://{p.netloc.lower()}{path}"

# ----------------------------
# Product ID extraction
# ----------------------------
def extract_product_id_from_input_url(product_url: str) -> int | None:
    """
    Extract product id from supported input URL patterns.
    - Cafe24 A: /surl/p/{id}
    - Cafe24 B: /product/.../{id}/category/...  (id is right before '/category/')
    - Cafe24 C: /product/detail.html?product_no={id}
    - Imweb:    /Product/?idx={id}
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

    # Cafe24 C: /product/detail.html?product_no=819
    if path.rstrip("/").lower().endswith("/product/detail.html"):
        qs = parse_qs(query)
        if "product_no" in qs and qs["product_no"]:
            v = qs["product_no"][0]
            if v.isdigit():
                return int(v)

    # Imweb idx
    if path.rstrip("/").lower().endswith("/product"):
        qs = parse_qs(query)
        if "idx" in qs and qs["idx"]:
            v = qs["idx"][0]
            if v.isdigit():
                return int(v)

    return None

# ----------------------------
# Platform detection (+ scan template policy)
# ----------------------------
def detect_platform_from_product_url(product_url: str):
    """
    Supported patterns:
    - Cafe24:
        * /surl/p/{id}
        * /product/.../{id}/category/...
        * /product/detail.html?product_no={id}
      ✅ Policy: If detected as Cafe24, scanning MUST ALWAYS use /surl/p/{id}

    - Imweb:
        * /Product/?idx={id}
    """
    raw = ensure_scheme(product_url)
    clean = strip_query_fragment(raw)

    parsed_clean = urlparse(clean)
    parsed_raw = urlparse(raw)

    path = parsed_clean.path or ""
    query = parsed_raw.query or ""
    base = normalize_home(clean)

    # -----------------
    # Cafe24 (ALL CASES) -> always scan with /surl/p/{id}
    # -----------------
    if "/surl/p/" in path and re.search(r"/surl/p/\d+", path):
        return "cafe24", f"{base}/surl/p/{{id}}"

    if path.startswith("/product/") and re.search(r"/product/.+/\d+/category/", path):
        return "cafe24", f"{base}/surl/p/{{id}}"

    if path.rstrip("/").lower().endswith("/product/detail.html"):
        if re.search(r"(?:^|&)product_no=\d+(?:&|$)", query, re.IGNORECASE):
            return "cafe24", f"{base}/surl/p/{{id}}"

    # -----------------
    # Imweb
    # -----------------
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
        #r'<meta[^>]+property=["\']og:title["\'][^>]*content=["\']([^"\']+)["\']',
        #r'<meta[^>]+name=["\']twitter:title["\'][^>]*content=["\']([^"\']+)["\']',
        r"<title[^>]*>(.*?)</title>",
    ]
    for pat in patterns:
        m = re.search(pat, html, re.IGNORECASE | re.DOTALL)
        if m:
            return clean_text(m.group(1))
    return "(제품명 추출 실패)"

def extract_influencer_names(product_names: list[str]) -> list[str]:
    """
    제품명 리스트에서 "네", "맘", "약"이 포함된 단어를 추출.
    예: "서울네약국 프리미엄" -> ["서울네약국"]
    """
    influencers = set()
    target_chars = ['네', '맘', '약']
    
    for name in product_names:
        # 공백과 특수문자로 단어 분리
        words = re.findall(r'[가-힣a-zA-Z0-9]+', name)
        for word in words:
            # 단어에 "네", "맘", "약" 중 하나라도 포함되면 추가
            if any(char in word for char in target_chars):
                influencers.add(word)
    
    return sorted(list(influencers))

# ----------------------------
# Not-found 판단 (✅ 원래 아이디어대로: 홈/인덱스 리다이렉트는 NOT FOUND)
# ----------------------------
def looks_not_found(status_code: int, requested_url: str, final_url: str, html: str) -> bool:
    if status_code != 200:
        return True

    # ✅ 없는 상품이면 홈/인덱스 계열로 리다이렉트되는 케이스
    req = normalize_for_compare(requested_url)
    fin = normalize_for_compare(final_url)
    if req != fin and is_homepage(final_url):
        return True

    sample = (html[:20000] or "").lower()
    for kw in NOT_FOUND_KEYWORDS:
        if kw in sample:
            return True

    if len(sample.strip()) < 200:
        return True

    return False

# ----------------------------
# File I/O for domain-based tracking
# ----------------------------
def get_last_id_from_file(domain: str) -> int:
    """
    도메인명.txt 파일에서 마지막 ID를 읽어옴.
    파일이 없거나 읽기 실패시 0을 반환.
    """
    filename = f"{domain}.txt"
    if not os.path.exists(filename):
        return 0
    
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            if not lines:
                return 0
            # 마지막 라인에서 ID 추출 (형식: "번호. 제품명\nURL")
            for line in reversed(lines):
                line = line.strip()
                if line.startswith('http'):
                    # URL에서 ID 추출
                    match = re.search(r'/(\d+)(?:[/?#]|$)', line)
                    if match:
                        return int(match.group(1))
            return 0
    except Exception as e:
        print(f"[WARNING] {domain}.txt 파일 읽기 실패: {e}")
        return 0

def load_existing_products(domain: str) -> list[tuple[str, str]]:
    """
    도메인명.txt 파일에서 기존 제품 목록을 로드.
    """
    filename = f"{domain}.txt"
    if not os.path.exists(filename):
        return []
    
    products = []
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            i = 0
            while i < len(lines):
                line = lines[i].strip()
                # "번호. 제품명" 형식 찾기
                if re.match(r'^\d+\.\s+', line):
                    name = re.sub(r'^\d+\.\s+', '', line)
                    # 다음 줄이 URL인지 확인
                    if i + 1 < len(lines):
                        url_line = lines[i + 1].strip()
                        if url_line.startswith('http'):
                            products.append((name, url_line))
                            i += 2
                            continue
                i += 1
    except Exception as e:
        print(f"[WARNING] {domain}.txt 파일 로드 실패: {e}")
    
    return products

def save_products_to_file(domain: str, products: list[tuple[str, str]]):
    """
    제품 목록을 도메인명.txt 파일로 저장.
    """
    filename = f"{domain}.txt"
    try:
        with open(filename, 'w', encoding='utf-8') as f:
            for idx, (name, url) in enumerate(products, 1):
                f.write(f"{idx}. {name}\n")
                f.write(f"{url}\n\n")
        print(f"[INFO] {len(products)}개 제품을 {filename}에 저장했습니다.")
    except Exception as e:
        print(f"[ERROR] {filename} 저장 실패: {e}")

def save_influencers_to_file(domain: str, influencers: list[str]) -> str:
    """
    인플루언서명을 도메인_influencers.txt 파일로 저장하고 파일명 반환.
    """
    filename = f"{domain}_influencers.txt"
    try:
        with open(filename, 'w', encoding='utf-8') as f:
            f.write("# 인플루언서명 추출 결과\n")
            f.write("# '네', '맘', '약'이 포함된 단어들\n\n")
            for inf in influencers:
                f.write(f"{inf}\n")
        print(f"[INFO] {len(influencers)}개 인플루언서명을 {filename}에 저장했습니다.")
        return filename
    except Exception as e:
        print(f"[ERROR] {filename} 저장 실패: {e}")
        return ""

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

    ✅ 추가 보호:
    - 연속 STOP_AFTER_CONSECUTIVE_HITS(기본 200)번 FOUND가 나오면 비정상으로 보고 에러 발생
      (예: 모든 요청이 어떤 공통 페이지로 "FOUND"로 판정되는 경우)
    """
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    product_id = start_id
    consecutive_misses = 0
    consecutive_hits = 0
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
                consecutive_hits = 0
                print(f"  -> NOT FOUND ({consecutive_misses}/{stop_after_consecutive_misses})")
            else:
                consecutive_misses = 0
                consecutive_hits += 1

                final_url = r.url
                p_name = ""
                if final_url not in found_urls:
                    name = extract_product_name(r.text or "")
                    found_products.append((name, final_url))
                    found_urls.add(final_url)
                    p_name = name

                print(f"  ✅ FOUND: {p_name}\n{final_url} ({consecutive_hits}/{STOP_AFTER_CONSECUTIVE_HITS})")

                # ✅ 비정상 감지: 연속으로 너무 많이 FOUND
                if consecutive_hits >= STOP_AFTER_CONSECUTIVE_HITS:
                    raise RuntimeError(
                        f"비정상 감지: 연속 {STOP_AFTER_CONSECUTIVE_HITS}개가 'FOUND'로 판정되었습니다. "
                        f"NOT FOUND 판정이 잘못되었거나 모든 요청이 공통 페이지로 리다이렉트되는 상황일 수 있습니다. "
                        f"(예: 마지막 요청 URL: {url}, 최종 URL: {final_url})"
                    )

        except requests.RequestException as e:
            consecutive_misses += 1
            consecutive_hits = 0
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
    print("예) https://brainology.kr/product/.../10/category/24/display/1/  (카페24 감지용, 스캔은 /surl/p/{id})")
    print("예) https://drphytomall.com/product/detail.html?product_no=819  (카페24 감지용, 스캔은 /surl/p/{id})")
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
    print(f"[INFO] 비정상 기준: 연속 {STOP_AFTER_CONSECUTIVE_HITS}회 FOUND면 에러")
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
    threshold = input_product_id * 0.01
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
            allow_extra_retry_if_zero_found=False,
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
    - 도메인별 .txt 파일에서 마지막 ID 확인
    - 스캔 후 결과를 .txt 파일에 저장
    - 기존 + 신규 제품 모두 반환
    """
    platform, template_url = detect_platform_from_product_url(product_url)
    if not platform:
        raise ValueError("Unsupported product URL pattern")

    input_product_id = extract_product_id_from_input_url(product_url)
    if input_product_id is None:
        raise ValueError("Failed to extract product id from URL")

    # 도메인 추출
    domain = get_domain_from_url(product_url)
    
    # 기존 제품 로드
    existing_products = load_existing_products(domain)
    existing_urls = {url for _, url in existing_products}
    
    # 마지막 ID 확인
    last_id = get_last_id_from_file(domain)
    start_id = last_id + 1 if last_id > 0 else 1
    
    print(f"[INFO] 도메인: {domain}")
    print(f"[INFO] 기존 제품 수: {len(existing_products)}")
    print(f"[INFO] 마지막 ID: {last_id}")
    print(f"[INFO] 스캔 시작 ID: {start_id}")

    # 스캔 시작
    found_products, found_urls = scan_pass(
        template_url=template_url,
        start_id=start_id,
        stop_after_consecutive_misses=STOP_AFTER_CONSECUTIVE_MISSES,
        sleep_sec=SLEEP_SEC,
        allow_extra_retry_if_zero_found=True,
        found_products=[],
        found_urls=existing_urls.copy(),
    )

    # 2차 스캔 조건 확인 (입력 ID가 start_id보다 크고, 발견 수가 적을 때)
    if input_product_id > start_id and len(found_products) < (input_product_id * 0.01):
        found_products, found_urls = scan_pass(
            template_url=template_url,
            start_id=input_product_id,
            stop_after_consecutive_misses=STOP_AFTER_CONSECUTIVE_MISSES,
            sleep_sec=SLEEP_SEC,
            allow_extra_retry_if_zero_found=False,
            found_products=found_products,
            found_urls=found_urls,
        )

    # 기존 + 신규 제품 합치기
    all_products = existing_products + found_products
    
    # 파일에 저장 (전체 제품)
    save_products_to_file(domain, all_products)
    
    # 인플루언서명 추출
    all_product_names = [name for name, _ in all_products]
    influencers = extract_influencer_names(all_product_names)
    
    # 인플루언서명 파일 저장
    influencer_file = save_influencers_to_file(domain, influencers)
    
    # 신규 제품 수 출력
    print(f"[INFO] 신규 발견 제품: {len(found_products)}개")
    print(f"[INFO] 전체 제품: {len(all_products)}개")
    print(f"[INFO] 인플루언서명: {len(influencers)}개")

    return all_products, found_products, influencer_file

if __name__ == "__main__":
    main()
