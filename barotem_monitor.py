"""
바로템(barotem.com) 상품 목록 모니터링 스크립트
- 지정된 검색 조건의 상품 목록을 주기적으로 확인
- 새로운 상품이 등록되면 텔레그램 + Windows 알림

사용법 (로컬):
  py barotem_monitor.py          # 기본 60초 간격
  py barotem_monitor.py 120      # 120초 간격
  py barotem_monitor.py --once   # 1회만 실행 (테스트용)

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
CI_LOOP_COUNT = 5    # CI 모드에서 반복 횟수
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(SCRIPT_DIR, "barotem_data.json")
LOG_FILE = os.path.join(SCRIPT_DIR, "barotem_log.txt")

# ─── 텔레그램 설정 (환경변수에서 읽기) ──────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


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
        options.binary_location = "/usr/bin/google-chrome"
        service = Service("/usr/bin/chromedriver")
    else:
        options.add_experimental_option("excludeSwitches", ["enable-logging"])
        from webdriver_manager.chrome import ChromeDriverManager
        service = Service(ChromeDriverManager().install())

    return webdriver.Chrome(service=service, options=options)


# ─── 텔레그램 메시지 전송 ──────────────────────────────
def send_telegram(text):
    """텔레그램 봇으로 메시지 전송"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log("[텔레그램] 토큰/챗ID 미설정. 환경변수 TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID 필요.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text[:4096],
        "parse_mode": "HTML",
    }).encode()

    req = urllib.request.Request(url, data=data)

    try:
        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read().decode())
            if result.get("ok"):
                log("[텔레그램] 메시지 전송 성공")
                return True
            else:
                log(f"[텔레그램] 전송 실패: {result}")
                return False
    except Exception as e:
        log(f"[텔레그램] 전송 오류: {e}")
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


# ─── 알림 통합 (텔레그램 + Windows) ────────────────────
def notify_all(new_products):
    """새 상품 발견 시 모든 채널로 알림"""
    notify_windows(new_products)

    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        for p in new_products:
            msg = (
                f"<b>🔔 바로템 새 상품</b>\n"
                f"서버: {p['server']} | {p['category']}\n"
                f"가격: <b>{p['price']}</b>\n"
                f"{p['description'][:150]}\n"
                f"<a href=\"{p['url']}\">상품 보기</a>"
            )
            send_telegram(msg)
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
    ci_mode = "--ci" in sys.argv
    once_mode = "--once" in sys.argv

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

    has_telegram = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)
    log(f"텔레그램: {'연결됨' if has_telegram else '미설정'}")

    driver = create_driver()
    log("Chrome 헤드리스 브라우저 시작됨")

    try:
        if once_mode:
            check_once(driver)
            return

        if ci_mode:
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
