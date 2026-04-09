"""
바로템(barotem.com) 상품 목록 모니터링 스크립트
- 지정된 검색 조건의 상품 목록을 주기적으로 확인
- 새로운 상품이 등록되면 카카오톡 나에게 보내기 + Windows 알림

사용법 (로컬):
  py barotem_monitor.py          # 기본 60초 간격
  py barotem_monitor.py 120      # 120초 간격
  py barotem_monitor.py --once   # 1회만 실행 (테스트용)
  py barotem_monitor.py --auth   # 카카오톡 토큰 발급 (최초 1회)

사용법 (GitHub Actions CI):
  python barotem_monitor.py --ci          # 1분 간격 5회 체크
  python barotem_monitor.py --ci --loop 5 # 반복 횟수 지정
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# ─── 설정 ───────────────────────────────────────────────
TARGET_URL = (
    "https://www.barotem.com/product/lists/1"
    "?page=1&sell=sell&category=1r10&display=2&orderby=1"
    "&minpay=70&maxpay=180&search_word=&brand=&buyloc="
    "&opt1=20%2C21%2C22%2C23%2C24%2C25%2C26%2C27%2C28%2C29"
    "%2C30%2C31%2C32%2C33%2C34%2C35%2C36%2C37%2C786%2C4601"
    "&opt2=&opt3=&opt4=&opt5=&opt6=&opt7=&opt8=&opt9=&opt10="
)

CHECK_INTERVAL = 60  # 확인 주기 (초)
CI_LOOP_COUNT = 5    # CI 모드에서 반복 횟수 (5분 워크플로우 / 1분 간격)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(SCRIPT_DIR, "barotem_data.json")
LOG_FILE = os.path.join(SCRIPT_DIR, "barotem_log.txt")
KAKAO_TOKEN_FILE = os.path.join(SCRIPT_DIR, "kakao_token.json")

# ─── 카카오톡 설정 (로컬용 / CI에서는 환경변수 사용) ────
KAKAO_REST_API_KEY = os.environ.get("KAKAO_REST_API_KEY", "")
KAKAO_CLIENT_SECRET = os.environ.get("KAKAO_CLIENT_SECRET", "")
KAKAO_REDIRECT_URI = "http://localhost:9999/callback"


# ─── 브라우저 설정 ──────────────────────────────────────
def create_driver():
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--log-level=3")

    ci_mode = "--ci" in sys.argv or os.environ.get("CI") == "true"

    if ci_mode:
        # GitHub Actions: 시스템 Chrome + chromedriver 사용
        options.binary_location = "/usr/bin/google-chrome"
        service = Service("/usr/bin/chromedriver")
    else:
        # 로컬: webdriver-manager 사용
        options.add_experimental_option("excludeSwitches", ["enable-logging"])
        from webdriver_manager.chrome import ChromeDriverManager
        service = Service(ChromeDriverManager().install())

    return webdriver.Chrome(service=service, options=options)


# ─── 카카오톡 인증 (로컬 전용) ─────────────────────────
def kakao_auth():
    """카카오톡 OAuth 인증 → access_token 발급 및 저장"""
    if not KAKAO_REST_API_KEY:
        print("=" * 50)
        print("카카오톡 API 설정이 필요합니다!")
        print()
        print("1. https://developers.kakao.com 접속 → 로그인")
        print("2. [내 애플리케이션] → [애플리케이션 추가하기]")
        print("3. 앱 이름: '바로템 모니터' 등 자유롭게 입력")
        print("4. [앱 키] → REST API 키 복사")
        print("5. 환경변수 설정: set KAKAO_REST_API_KEY=발급받은키")
        print("   또는 이 파일의 KAKAO_REST_API_KEY 직접 수정")
        print("6. [플랫폼] → [Web] → 사이트 도메인: http://localhost:9999")
        print("7. [카카오 로그인] → 활성화 ON")
        print("8. [동의항목] → '카카오톡 메시지 전송' → 선택 동의")
        print("9. [카카오 로그인] → [Redirect URI] → http://localhost:9999/callback 추가")
        print()
        print("설정 완료 후 다시 --auth 를 실행하세요.")
        print("=" * 50)
        return

    auth_url = (
        "https://kauth.kakao.com/oauth/authorize"
        f"?client_id={KAKAO_REST_API_KEY}"
        f"&redirect_uri={urllib.parse.quote(KAKAO_REDIRECT_URI)}"
        "&response_type=code"
        "&scope=talk_message"
    )

    print("=" * 50)
    print("카카오톡 인증을 시작합니다.")
    print()
    print("아래 URL을 브라우저에서 열어 로그인하세요:")
    print(auth_url)
    print()

    import http.server
    import socketserver

    auth_code = None

    class CallbackHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            nonlocal auth_code
            query = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(query)
            auth_code = params.get("code", [None])[0]
            self.send_response(200)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("인증 완료! 이 창을 닫아도 됩니다.".encode("utf-8"))

        def log_message(self, format, *args):
            pass

    with socketserver.TCPServer(("", 9999), CallbackHandler) as httpd:
        print("로그인 대기 중... (http://localhost:9999)")
        httpd.handle_request()

    if not auth_code:
        print("[오류] 인증 코드를 받지 못했습니다.")
        return

    print(f"인증 코드 수신: {auth_code[:10]}...")

    token_params = {
        "grant_type": "authorization_code",
        "client_id": KAKAO_REST_API_KEY,
        "redirect_uri": KAKAO_REDIRECT_URI,
        "code": auth_code,
    }
    if KAKAO_CLIENT_SECRET:
        token_params["client_secret"] = KAKAO_CLIENT_SECRET
    token_data = urllib.parse.urlencode(token_params).encode()

    req = urllib.request.Request(
        "https://kauth.kakao.com/oauth/token",
        data=token_data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    try:
        with urllib.request.urlopen(req) as resp:
            token_info = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        error_body = e.read().decode()
        print(f"[오류] HTTP {e.code}: {error_body}")
        print(f"[디버그] client_id 길이: {len(KAKAO_REST_API_KEY)}, 값: {KAKAO_REST_API_KEY[:8]}...")
        return

    token_info["issued_at"] = datetime.now().isoformat()
    with open(KAKAO_TOKEN_FILE, "w", encoding="utf-8") as f:
        json.dump(token_info, f, ensure_ascii=False, indent=2)

    print(f"토큰 저장 완료: {KAKAO_TOKEN_FILE}")
    print(f"access_token 유효기간: {token_info.get('expires_in', '?')}초")
    print(f"refresh_token 유효기간: {token_info.get('refresh_token_expires_in', '?')}초")
    print()
    print("GitHub Actions 사용 시 아래 값을 Secrets에 등록하세요:")
    print(f"  KAKAO_ACCESS_TOKEN  = {token_info.get('access_token', '')[:20]}...")
    print(f"  KAKAO_REFRESH_TOKEN = {token_info.get('refresh_token', '')[:20]}...")
    print("=" * 50)


# ─── 카카오톡 토큰 관리 ────────────────────────────────
def load_kakao_token():
    """카카오 토큰 로드. 환경변수 → 파일 순으로 확인. 만료 시 refresh."""

    # CI 환경: 환경변수에서 읽기
    env_token = os.environ.get("KAKAO_ACCESS_TOKEN")
    env_refresh = os.environ.get("KAKAO_REFRESH_TOKEN")

    if env_token:
        # 환경변수의 토큰은 항상 refresh 시도 (만료 여부 판단 불가)
        if env_refresh and KAKAO_REST_API_KEY:
            refreshed = _refresh_token(env_refresh)
            if refreshed:
                return refreshed
        return env_token

    # 로컬: 파일에서 읽기
    if not os.path.exists(KAKAO_TOKEN_FILE):
        return None

    with open(KAKAO_TOKEN_FILE, "r", encoding="utf-8") as f:
        token_info = json.load(f)

    issued = datetime.fromisoformat(token_info.get("issued_at", "2000-01-01"))
    expires_in = token_info.get("expires_in", 0)
    elapsed = (datetime.now() - issued).total_seconds()

    if elapsed > expires_in - 60:
        refresh_token = token_info.get("refresh_token")
        if not refresh_token or not KAKAO_REST_API_KEY:
            log("[카카오] 토큰 만료. --auth 로 재인증 필요.")
            return None

        new_access = _refresh_token(refresh_token)
        if new_access:
            # 파일 업데이트
            token_info["access_token"] = new_access
            token_info["issued_at"] = datetime.now().isoformat()
            with open(KAKAO_TOKEN_FILE, "w", encoding="utf-8") as f:
                json.dump(token_info, f, ensure_ascii=False, indent=2)
            return new_access
        return None

    return token_info.get("access_token")


def _refresh_token(refresh_token):
    """refresh_token으로 access_token 갱신"""
    try:
        refresh_params = {
            "grant_type": "refresh_token",
            "client_id": KAKAO_REST_API_KEY,
            "refresh_token": refresh_token,
        }
        if KAKAO_CLIENT_SECRET:
            refresh_params["client_secret"] = KAKAO_CLIENT_SECRET
        refresh_data = urllib.parse.urlencode(refresh_params).encode()

        req = urllib.request.Request(
            "https://kauth.kakao.com/oauth/token",
            data=refresh_data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        with urllib.request.urlopen(req) as resp:
            new_token = json.loads(resp.read().decode())

        log("[카카오] 토큰 갱신 완료")

        # CI: 갱신된 토큰을 kakao_token.json에도 저장 (커밋용)
        if os.environ.get("CI") == "true":
            save_obj = {
                "access_token": new_token["access_token"],
                "refresh_token": new_token.get("refresh_token", refresh_token),
                "expires_in": new_token.get("expires_in", 21599),
                "issued_at": datetime.now().isoformat(),
            }
            with open(KAKAO_TOKEN_FILE, "w", encoding="utf-8") as f:
                json.dump(save_obj, f, ensure_ascii=False, indent=2)

        return new_token.get("access_token")
    except Exception as e:
        log(f"[카카오] 토큰 갱신 실패: {e}")
        return None


def send_kakao_message(text, link_url=""):
    """카카오톡 나에게 보내기 API로 메시지 전송"""
    access_token = load_kakao_token()
    if not access_token:
        log("[카카오] 토큰 없음. 메시지 전송 건너뜀.")
        return False

    template = {
        "object_type": "text",
        "text": text[:300],
        "link": {
            "web_url": link_url or TARGET_URL,
            "mobile_web_url": link_url or TARGET_URL,
        },
        "button_title": "상품 보기",
    }

    post_data = urllib.parse.urlencode({
        "template_object": json.dumps(template, ensure_ascii=False),
    }).encode()

    req = urllib.request.Request(
        "https://kapi.kakao.com/v2/api/talk/memo/default/send",
        data=post_data,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )

    try:
        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read().decode())
            if result.get("result_code") == 0:
                log("[카카오] 메시지 전송 성공")
                return True
            else:
                log(f"[카카오] 전송 실패: {result}")
                return False
    except Exception as e:
        log(f"[카카오] 전송 오류: {e}")
        return False


# ─── 상품 목록 수집 ─────────────────────────────────────
def fetch_products(driver):
    """페이지에서 상품 목록 수집. <a class="newlists_goods_content" id="상품번호">"""
    driver.get(TARGET_URL)

    try:
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, "a.newlists_goods_content")
            )
        )
    except Exception:
        log("[경고] 상품 목록 로딩 타임아웃")

    time.sleep(2)

    elements = driver.find_elements(By.CSS_SELECTOR, "a.newlists_goods_content")
    products = []

    for elem in elements:
        product_id = elem.get_attribute("id")
        if not product_id:
            continue

        try:
            li_elems = elem.find_elements(By.CSS_SELECTOR, "ul li")
            server = li_elems[0].text.strip() if len(li_elems) > 0 else ""
            category = li_elems[1].text.strip() if len(li_elems) > 1 else ""

            desc_elems = elem.find_elements(By.CSS_SELECTOR, "div > p")
            description = ""
            for p in desc_elems:
                cls = p.get_attribute("class") or ""
                if "onoffline" not in cls:
                    description = p.text.strip()
                    break

            price_elem = elem.find_elements(By.CSS_SELECTOR, "h3")
            price = price_elem[0].text.strip() if price_elem else ""

            divs = elem.find_elements(By.CSS_SELECTOR, ":scope > div")
            date_text = divs[-1].text.strip() if divs else ""

            products.append({
                "id": product_id,
                "server": server,
                "category": category,
                "description": description[:200],
                "price": price,
                "date": date_text,
                "url": f"https://www.barotem.com/product/view/{product_id}",
            })
        except Exception:
            products.append({
                "id": product_id,
                "server": "",
                "category": "",
                "description": elem.text.strip()[:200],
                "price": "",
                "date": "",
                "url": f"https://www.barotem.com/product/view/{product_id}",
            })

    return products


# ─── 데이터 저장/로드 ───────────────────────────────────
def load_saved_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"known_ids": [], "products": []}


def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ─── 로그 ──────────────────────────────────────────────
def log(msg):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ─── Windows 알림 (로컬 전용) ──────────────────────────
def notify_windows(new_products):
    if os.environ.get("CI") == "true":
        return
    try:
        from winotify import Notification
        if len(new_products) == 1:
            p = new_products[0]
            toast = Notification(
                app_id="바로템 모니터",
                title=f"새 상품! {p['price']}",
                msg=f"[{p['server']} {p['category']}] {p['description'][:100]}",
                launch=p["url"],
            )
            toast.show()
        else:
            toast = Notification(
                app_id="바로템 모니터",
                title=f"새 상품 {len(new_products)}개 등록!",
                msg="\n".join(f"• {p['description'][:50]}" for p in new_products[:3]),
                launch=TARGET_URL,
            )
            toast.show()
    except Exception as e:
        log(f"[알림 오류] {e}")


# ─── 알림 통합 ─────────────────────────────────────────
def notify_all(new_products):
    """새 상품 발견 시 모든 채널로 알림"""
    notify_windows(new_products)

    # 카카오톡 나에게 보내기
    has_token = (
        os.path.exists(KAKAO_TOKEN_FILE)
        or os.environ.get("KAKAO_ACCESS_TOKEN")
    )
    if has_token:
        for p in new_products:
            msg = (
                f"[바로템 새 상품]\n"
                f"서버: {p['server']} | {p['category']}\n"
                f"가격: {p['price']}\n"
                f"{p['description'][:150]}"
            )
            send_kakao_message(msg, p["url"])
            time.sleep(0.3)


# ─── 1회 체크 ─────────────────────────────────────────
def check_once(driver):
    """상품 목록 1회 확인. 새 상품 발견 시 True 반환."""
    log("상품 목록 확인 중...")
    products = fetch_products(driver)
    log(f"수집된 상품 수: {len(products)}")

    if not products:
        log("[경고] 상품을 가져오지 못했습니다.")
        return False

    saved = load_saved_data()
    known_ids = set(saved["known_ids"])

    if not known_ids:
        # 첫 실행
        saved["known_ids"] = [p["id"] for p in products]
        saved["products"] = products
        save_data(saved)
        log(f"초기 데이터 저장 완료: {len(products)}개 상품")
        for p in products:
            log(f"  [{p['id']}] {p['server']} {p['category']} | {p['price']} | {p['description'][:60]}")
        return False

    new_products = [p for p in products if p["id"] not in known_ids]

    if new_products:
        log(f"★★★ 새 상품 {len(new_products)}개 발견! ★★★")
        for p in new_products:
            log(f"  → [{p['id']}] {p['server']} {p['category']}")
            log(f"    {p['description'][:100]}")
            log(f"    가격: {p['price']}")
            log(f"    링크: {p['url']}")

        notify_all(new_products)

        for p in new_products:
            saved["known_ids"].append(p["id"])
        saved["products"] = products
        save_data(saved)
        return True
    else:
        log("변경 없음.")
        return False


# ─── 메인 ─────────────────────────────────────────────
def main():
    if "--auth" in sys.argv:
        kakao_auth()
        return

    ci_mode = "--ci" in sys.argv
    once_mode = "--once" in sys.argv

    # --loop N 파싱
    loop_count = CI_LOOP_COUNT
    if "--loop" in sys.argv:
        idx = sys.argv.index("--loop")
        if idx + 1 < len(sys.argv):
            loop_count = int(sys.argv[idx + 1])

    args = [a for a in sys.argv[1:] if not a.startswith("--") and a not in [str(loop_count)]]
    interval = int(args[0]) if args else CHECK_INTERVAL

    log("=" * 50)
    log("바로템 상품 모니터링 시작")

    if ci_mode:
        log(f"모드: CI ({loop_count}회 반복, {interval}초 간격)")
    elif once_mode:
        log("모드: 1회 실행")
    else:
        log(f"모드: 상시 실행 ({interval}초 간격)")

    log(f"URL: {TARGET_URL[:80]}...")

    has_kakao = bool(
        os.environ.get("KAKAO_ACCESS_TOKEN")
        or os.path.exists(KAKAO_TOKEN_FILE)
    )
    log(f"카카오톡: {'연결됨' if has_kakao else '미설정'}")

    driver = create_driver()
    log("Chrome 헤드리스 브라우저 시작됨")

    try:
        if once_mode:
            check_once(driver)
            return

        if ci_mode:
            # CI: 정해진 횟수만 반복
            for i in range(loop_count):
                log(f"--- [{i+1}/{loop_count}] ---")
                try:
                    check_once(driver)
                except Exception as e:
                    log(f"[오류] {e}")
                if i < loop_count - 1:
                    log(f"다음 확인까지 {interval}초 대기...")
                    time.sleep(interval)
            return

        # 로컬: 무한 루프
        while True:
            try:
                check_once(driver)
            except Exception as e:
                log(f"[오류] {e}")
                try:
                    driver.quit()
                except Exception:
                    pass
                driver = create_driver()
                log("브라우저 재시작됨")

            log(f"다음 확인까지 {interval}초 대기...\n")
            time.sleep(interval)

    except KeyboardInterrupt:
        log("\n모니터링 종료 (Ctrl+C)")
    finally:
        driver.quit()
        log("브라우저 종료됨")


if __name__ == "__main__":
    main()
