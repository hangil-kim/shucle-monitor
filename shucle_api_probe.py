"""
셔클 인사이트 API 탐색 스크립트
================================
목적: 각 탭에서 어떤 API가 호출되는지, 응답 구조가 어떤지 파악
결과: shucle_data/api_probe/ 폴더에 탭별 API 응답 저장
→ 이 결과를 Claude에 공유하면 본 수집 스크립트를 정확하게 만들 수 있음

실행: python shucle_api_probe.py
사전: pip install playwright && playwright install chromium
"""

import asyncio, os, json, re, sys, urllib.parse
from datetime import datetime
from playwright.async_api import async_playwright

# stdout UTF-8 강제 설정 (Windows cp949 이모지 오류 방지)
sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SITE_URL = "https://insight.shucle.com/metrics"
BROWSER_PROFILE = "./shucle_browser_profile"
BASE_DATA_DIR = "shucle_data"

TABS = ["호출 탑승", "서비스 품질", "가호출 수요", "지역 회원", "정류장 이용", "차량 운행"]

# 관심 있는 API 패턴 (Grafana, 커스텀 API 등)
API_PATTERNS = [
    "api/ds/query",    # Grafana datasource query
    "api/query",       # 일반 쿼리
    "/query",          # 쿼리 엔드포인트
    "grafana",         # Grafana 관련
    "/api/",           # 일반 API
    "metrics",         # 메트릭 관련
    "dashboard",       # 대시보드 관련
    "panels",          # 패널 데이터
    "tsdb",            # 시계열 DB
    "prometheus",      # Prometheus
    "influx",          # InfluxDB
    "elasticsearch",   # ES
]

# 로그인 대기 최대 시간 (초)
LOGIN_TIMEOUT = 300
# 지역 선택 자동 재시도 횟수
REGION_RETRY = 3
# 지역 선택 실패 시 대기 시간 (초) — 수동 선택 여유
REGION_MANUAL_WAIT = 30

# ============================================================
# 실행 시 입력받는 설정값 (전역)
# ============================================================
REGION_KEYWORD = ""
DATE_RANGE = ""


def prompt_settings():
    """실행 시 지역과 기간을 입력받아 전역 설정에 반영"""
    global REGION_KEYWORD, DATE_RANGE

    print("\n[설정] 수집 파라미터 입력")
    print("-" * 40)

    # 지역 입력 (필수)
    while True:
        region = input("  지역 키워드 (예: 검단, 영덕): ").strip()
        if region:
            REGION_KEYWORD = region
            break
        print("  -> 지역을 입력해주세요!")

    # 기간 입력 (필수)
    print("  기간 형식:")
    print("    프리셋: 1주 / 4주 / 12주")
    print("    커스텀: 2026-01-01,2026-02-15")
    while True:
        period = input("  기간: ").strip()
        if not period:
            print("  -> 기간을 입력해주세요!")
            continue
        if period in ("1주", "4주", "12주"):
            DATE_RANGE = period
            break
        # 커스텀 날짜 파싱 시도
        parts = [p.strip() for p in period.split(",")]
        if len(parts) == 2:
            try:
                datetime.strptime(parts[0], "%Y-%m-%d")
                datetime.strptime(parts[1], "%Y-%m-%d")
                DATE_RANGE = (parts[0], parts[1])
                break
            except ValueError:
                pass
        print("  -> 올바른 형식으로 입력해주세요! (1주 / 4주 / 12주 / YYYY-MM-DD,YYYY-MM-DD)")

    print(f"\n  => 지역: {REGION_KEYWORD}")
    print(f"  => 기간: {DATE_RANGE}")
    print("-" * 40)


async def select_region(page, region_keyword=None):
    """
    지역 자동 선택.
    UI 구조: 상단에 zone-select 드롭다운 버튼(aria-haspopup="dialog")이 있고,
    클릭하면 팝오버에 지역 목록이 나타남. 각 지역은 zone-select__Value 클래스의 div.
    """
    if region_keyword is None:
        region_keyword = REGION_KEYWORD
    print(f"[지역] 지역 선택: {region_keyword}")

    # ---------- 1) 이미 해당 지역인지 확인 ----------
    try:
        # 지역 드롭다운 버튼 (두 번째 aria-haspopup="dialog" 버튼이 지역 선택)
        zone_buttons = page.locator('button[data-slot="trigger"][aria-haspopup="dialog"]')
        cnt = await zone_buttons.count()
        for idx in range(cnt):
            btn = zone_buttons.nth(idx)
            try:
                txt = await btn.inner_text(timeout=1000)
                if region_keyword in txt:
                    print(f"   -> 이미 {region_keyword} 선택됨")
                    return True
            except Exception:
                continue
    except Exception:
        pass

    # ---------- 2) 지역 드롭다운 열기 ----------
    # aria-haspopup 버튼 중 DRT/유형/달력이 아닌 것 = 지역 선택 버튼
    zone_trigger = None
    try:
        triggers = page.locator('button[data-slot="trigger"][aria-haspopup="dialog"]')
        cnt = await triggers.count()
        for idx in range(cnt):
            btn = triggers.nth(idx)
            try:
                txt = await btn.inner_text(timeout=2000)
            except Exception:
                continue
            if "DRT" not in txt and "전체 유형" not in txt and "달력" not in txt and txt.strip():
                zone_trigger = btn
                print(f"   현재 지역: {txt.strip()}")
                break
    except Exception:
        pass

    if not zone_trigger:
        print("   [FAIL] 지역 드롭다운 버튼을 찾을 수 없음")
        return False

    # 드롭다운 열기
    await zone_trigger.click()
    await page.wait_for_timeout(2000)

    # ---------- 3) Options(전체주소) 에서 키워드 매칭 → 대응 Values(표시명) 클릭 ----------
    # UI 구조: Options = div[class*="zone-select__Option"], 전체 주소 (예: "전라남도영암군삼호")
    #          Values  = div[class*="zone-select__Value"], 표시명 (예: "삼호")
    #          검색 기능은 필터링하지 않으므로, Options 텍스트에서 키워드 포함 여부로 매칭
    clicked = False
    js_result = await page.evaluate("""(keyword) => {
        // Options: 개별 옵션 (컨테이너=높이 200+ 제외)
        const allOptions = document.querySelectorAll('div[class*="zone-select__Option"]');
        const options = [];
        for (const el of allOptions) {
            const text = el.textContent?.trim() || '';
            const h = el.getBoundingClientRect().height;
            if (h > 5 && h < 200 && text.length > 0 && text.length <= 50) {
                options.push({el, text});
            }
        }
        // Values: 표시명
        const allValues = document.querySelectorAll('div[class*="zone-select__Value"]');
        const values = [];
        for (const el of allValues) {
            const text = el.textContent?.trim() || '';
            const h = el.getBoundingClientRect().height;
            const style = window.getComputedStyle(el);
            if (h > 5 && style.display !== 'none' && text.length > 0 && text.length <= 30) {
                values.push({el, text});
            }
        }
        // 1순위: Values(표시명)에 키워드 직접 포함 → 즉시 클릭
        for (const v of values) {
            if (v.text.includes(keyword)) {
                v.el.click();
                return {success: true, text: v.text, option: '', method: 'keyword-in-value'};
            }
        }
        // 2순위: Options(전체주소)에서 키워드 포함 찾기 → 같은 인덱스 Value 클릭
        for (let i = 0; i < options.length; i++) {
            if (options[i].text.includes(keyword)) {
                if (i < values.length) {
                    values[i].el.click();
                    return {success: true, text: values[i].text, option: options[i].text, method: 'index'};
                }
                // 인덱스 대응 실패 시 suffix 매칭
                for (const v of values) {
                    if (options[i].text.endsWith(v.text)) {
                        v.el.click();
                        return {success: true, text: v.text, option: options[i].text, method: 'suffix'};
                    }
                }
            }
        }
        return {success: false, options: options.map(o => o.text), values: values.map(v => v.text)};
    }""", region_keyword)
    if js_result.get("success"):
        print(f"   [OK] '{js_result['text']}' 클릭 ({js_result['method']}, 주소: {js_result.get('option', '')})")
        clicked = True
    else:
        print(f"   [FAIL] 키워드 '{region_keyword}' 매칭 실패")
        print(f"   Options: {js_result.get('options', [])}")
        print(f"   Values: {js_result.get('values', [])}")

    if not clicked:
        print(f"   [FAIL] '{region_keyword}' 옵션을 찾을 수 없음")
        await page.keyboard.press("Escape")
        return False

    # ---------- 5) 선택 완료 대기 ----------
    await page.wait_for_timeout(3000)

    # 선택 확인: 드롭다운 버튼 텍스트 변경 여부 확인
    try:
        triggers = page.locator('button[data-slot="trigger"][aria-haspopup="dialog"]')
        cnt = await triggers.count()
        for idx in range(cnt):
            btn = triggers.nth(idx)
            try:
                txt = await btn.inner_text(timeout=1000)
            except Exception:
                continue
            if "DRT" not in txt and "전체 유형" not in txt and "달력" not in txt and txt.strip():
                new_region = txt.strip().replace("\n", " ")
                print(f"   [확인] 지역 선택 완료: {new_region}")
                return True
    except Exception:
        pass

    # 버튼 텍스트 확인 불가해도 클릭 성공했으므로 진행
    try:
        await page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        pass
    await page.wait_for_timeout(3000)
    print(f"   [OK] 지역 선택 완료 (클릭 성공)")
    return True


async def set_date_segment(page, segment, target_value, dtype=""):
    """
    spinbutton 세그먼트에 값을 설정.
    React Aria spinbutton: 포커스 후 ArrowUp/Down 또는 숫자 키 입력.
    - year (차이 ≤10): ArrowUp/Down, (차이 >10): 숫자 키 입력
    - month/day: ArrowUp/Down
    - year 실패 시: triple-click + 직접 입력, fill(), JS 강제 설정 순차 시도
    """
    current = await segment.get_attribute("aria-valuenow")
    label = await segment.get_attribute("aria-label") or ""
    print(f"      [seg] {dtype} {label.strip()}: {current} → {target_value}", end="")
    if current == str(target_value):
        print(" (skip)")
        return  # 이미 원하는 값

    await segment.click()
    await page.wait_for_timeout(200)

    diff = target_value - int(current)

    if dtype == "year" and abs(diff) <= 10:
        # 년도 차이 작으면 ArrowUp/Down (숫자 키 입력보다 안정적)
        key = "ArrowUp" if diff > 0 else "ArrowDown"
        for _ in range(abs(diff)):
            await page.keyboard.press(key)
            await page.wait_for_timeout(100)
    elif dtype == "year" or abs(diff) > 50:
        # 년도 차이 크면 숫자 키 입력
        value_str = str(target_value)
        for digit in value_str:
            await page.keyboard.press(f"Digit{digit}")
            await page.wait_for_timeout(80)
    else:
        # 월/일은 ArrowUp/Down
        key = "ArrowUp" if diff > 0 else "ArrowDown"
        for _ in range(abs(diff)):
            await page.keyboard.press(key)
            await page.wait_for_timeout(50)

    await page.wait_for_timeout(200)
    after = await segment.get_attribute("aria-valuenow")

    # ── year 설정 실패 시 대안 방법 순차 시도 ──
    if dtype == "year" and str(after) != str(target_value):
        print(f" → FAIL(got {after}), 대안 시도...", end="")

        # 대안 1: triple-click + 숫자 직접 타이핑
        await segment.click(click_count=3)
        await page.wait_for_timeout(200)
        for digit in str(target_value):
            await page.keyboard.press(f"Digit{digit}")
            await page.wait_for_timeout(100)
        await page.keyboard.press("Tab")
        await page.wait_for_timeout(300)
        after = await segment.get_attribute("aria-valuenow")

        if str(after) != str(target_value):
            # 대안 2: Playwright fill() (contenteditable 요소)
            print(f" try2...", end="")
            try:
                await segment.fill(str(target_value))
                await page.wait_for_timeout(300)
                after = await segment.get_attribute("aria-valuenow")
            except Exception:
                pass

        if str(after) != str(target_value):
            # 대안 3: JS로 React Aria 내부 이벤트 시뮬레이션
            print(f" try3...", end="")
            try:
                await segment.evaluate("""(el, val) => {
                    el.focus();
                    // aria 속성 직접 설정
                    el.setAttribute('aria-valuenow', val);
                    el.textContent = val;
                    // React synthetic events 트리거
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    el.dispatchEvent(new Event('blur', { bubbles: true }));
                }""", str(target_value))
                await page.wait_for_timeout(300)
                after = await segment.get_attribute("aria-valuenow")
            except Exception:
                pass

        if str(after) != str(target_value):
            # 대안 4: Backspace로 지우고 다시 입력
            print(f" try4...", end="")
            await segment.click()
            await page.wait_for_timeout(100)
            await page.keyboard.press("Control+a")
            await page.wait_for_timeout(100)
            await page.keyboard.press("Backspace")
            await page.wait_for_timeout(100)
            await page.keyboard.type(str(target_value))
            await page.keyboard.press("Tab")
            await page.wait_for_timeout(300)
            after = await segment.get_attribute("aria-valuenow")

    ok = "OK" if str(after) == str(target_value) else f"FAIL(got {after})"
    print(f" → {ok}")

    await page.wait_for_timeout(200)


async def select_date_range(page, period="1주"):
    """
    기간 설정.
    - 프리셋: "1주" / "4주" / "12주" → 단축 버튼 클릭
    - 커스텀: ("2026-01-01", "2026-02-15") → 세그먼트 직접 입력
    """
    # ---------- 프리셋 단축 버튼 ----------
    if isinstance(period, str):
        print(f"[기간] 기간 설정: {period}")
        shortcuts = page.locator('div[class*="date-range-picker__Shortcut"]')
        try:
            cnt = await shortcuts.count()
            for idx in range(cnt):
                btn = shortcuts.nth(idx)
                txt = await btn.inner_text(timeout=1000)
                if txt.strip() == period:
                    await btn.click()
                    await page.wait_for_timeout(3000)
                    print(f"   [OK] '{period}' 선택 완료")
                    return True
            print(f"   [FAIL] '{period}' 버튼을 찾을 수 없음 (발견된 옵션: {cnt}개)")
            return False
        except Exception as e:
            print(f"   [FAIL] 기간 설정 실패: {e}")
            return False

    # ---------- 커스텀 날짜 범위 ----------
    if isinstance(period, (tuple, list)) and len(period) == 2:
        start_str, end_str = period
        print(f"[기간] 커스텀 날짜: {start_str} ~ {end_str}")

        # "2026-01-15" 형태 파싱
        try:
            s_parts = [int(x) for x in start_str.split("-")]  # [2026, 1, 15]
            e_parts = [int(x) for x in end_str.split("-")]    # [2026, 2, 15]
        except (ValueError, AttributeError):
            print(f"   [FAIL] 날짜 형식 오류 — 'YYYY-MM-DD' 형태로 입력하세요")
            return False

        # Step 0: 프리셋으로 시작 연도 맞추기 (시작일 연도 직접 변경 불가 문제 우회)
        # 시작일 year 세그먼트는 ArrowDown/digit 입력에 반응하지 않음
        # → 프리셋 버튼이 계산하는 "오늘" 날짜를 속여서 시작 연도를 맞춤
        #
        # 예: 시작=2025-10-01 필요 → Date.now()를 2026-01-01로 오버라이드
        #     → "12주" 클릭 시 12주 전 = 2025-10-09 → 시작 연도 2025 확보!
        today_year = datetime.now().year
        need_past_year = s_parts[0] < today_year

        if need_past_year:
            # 시작일이 과거 연도 → Date.now() 오버라이드로 프리셋 속이기
            # "12주" = 84일 전. 시작일이 target_start가 되려면
            # fake_today = target_start + 84일 (+ 여유 7일)
            from datetime import timedelta
            target_start = datetime(s_parts[0], s_parts[1], s_parts[2])
            fake_today = target_start + timedelta(days=91)  # 13주
            fake_ts = int(fake_today.timestamp() * 1000)
            print(f"   Date.now() 오버라이드: {fake_today.strftime('%Y-%m-%d')} (시작 연도 {s_parts[0]}년 확보)")

            # 페이지의 Date.now()와 new Date()를 오버라이드
            await page.evaluate(f"""() => {{
                const fakeNow = {fake_ts};
                const OrigDate = Date;
                const FakeDate = function(...args) {{
                    if (args.length === 0) return new OrigDate(fakeNow);
                    return new OrigDate(...args);
                }};
                FakeDate.now = () => fakeNow;
                FakeDate.parse = OrigDate.parse;
                FakeDate.UTC = OrigDate.UTC;
                FakeDate.prototype = OrigDate.prototype;
                window.Date = FakeDate;
                window.__originalDate = OrigDate;
            }}""")

            preset = "12주"
        else:
            preset = "1주"

        print(f"   프리셋 리셋 ({preset}) → 시작 연도 {s_parts[0]}년 확보")
        shortcuts = page.locator('div[class*="date-range-picker__Shortcut"]')
        try:
            cnt = await shortcuts.count()
            for idx in range(cnt):
                btn = shortcuts.nth(idx)
                txt = await btn.inner_text(timeout=1000)
                if txt.strip() == preset:
                    await btn.click()
                    await page.wait_for_timeout(3000)
                    break
        except Exception as e:
            print(f"   [WARN] 프리셋 리셋 실패: {e}")

        # Date.now() 복원 (프리셋 처리 완료 후)
        if need_past_year:
            await page.evaluate("""() => {
                if (window.__originalDate) {
                    window.Date = window.__originalDate;
                    delete window.__originalDate;
                }
            }""")
            print(f"   Date.now() 복원 완료")

        await page.wait_for_timeout(1000)

        # 세그먼트 찾기: role="spinbutton" + data-type + aria-label 시작일/종료일
        segments = page.locator('[data-slot="segment"][role="spinbutton"]')
        seg_count = await segments.count()
        if seg_count < 6:
            print(f"   [FAIL] 날짜 세그먼트 부족 ({seg_count}개, 최소 6개 필요)")
            return False

        # 세그먼트를 시작일/종료일 그룹으로 분류
        start_segs = {}  # {"year": locator, "month": locator, "day": locator}
        end_segs = {}
        for idx in range(seg_count):
            seg = segments.nth(idx)
            dtype = await seg.get_attribute("data-type")
            label = await seg.get_attribute("aria-label") or ""
            if "시작일" in label:
                start_segs[dtype] = seg
            elif "종료일" in label:
                end_segs[dtype] = seg

        if not all(k in start_segs for k in ("year", "month", "day")):
            print(f"   [FAIL] 시작일 세그먼트 누락: {list(start_segs.keys())}")
            return False
        if not all(k in end_segs for k in ("year", "month", "day")):
            print(f"   [FAIL] 종료일 세그먼트 누락: {list(end_segs.keys())}")
            return False

        # 프리셋 후 현재 시작/종료 연도 확인
        curr_start_year = int(await start_segs["year"].get_attribute("aria-valuenow") or today_year)
        curr_end_year = int(await end_segs["year"].get_attribute("aria-valuenow") or today_year)
        print(f"   프리셋 후 날짜: 시작={curr_start_year}년, 종료={curr_end_year}년")
        print(f"   목표: 시작={s_parts[0]}.{s_parts[1]}.{s_parts[2]}, 종료={e_parts[0]}.{e_parts[1]}.{e_parts[2]}")

        # 날짜 설정 순서 — "시작일 < 종료일" 제약을 항상 유지
        # Date.now() 오버라이드로 시작 연도는 이미 프리셋에서 맞춰짐
        # 종료 연도만 조정하면 됨
        #
        # 안전한 순서:
        # 1) 시작 월/일 최소화 (1/1) → 시작을 기간 내 최소값으로
        # 2) 종료 월/일 최대화 (12/31) → 공간 확보
        # 3) 종료 연도 조정 (필요 시)
        # 4) 시작 월/일 → 목표값
        # 5) 종료 월/일 → 목표값

        print(f"   시작일 임시 최소화 (1월 1일)")
        await set_date_segment(page, start_segs["month"], 1, "month")
        await set_date_segment(page, start_segs["day"], 1, "day")

        # 종료 연도 조정 (프리셋 결과와 목표가 다를 때)
        if curr_end_year != e_parts[0]:
            print(f"   종료 연도 조정: {curr_end_year} → {e_parts[0]}")
            await set_date_segment(page, end_segs["year"], e_parts[0], "year")

        # 종료 연도 조정
        if curr_end_year != e_parts[0]:
            print(f"   종료 연도 조정: {curr_end_year} → {e_parts[0]}")
            await set_date_segment(page, end_segs["year"], e_parts[0], "year")

        print(f"   종료일 설정: {end_str}")
        await set_date_segment(page, end_segs["month"], e_parts[1], "month")
        await set_date_segment(page, end_segs["day"], e_parts[2], "day")

        print(f"   시작일 설정: {start_str}")
        await set_date_segment(page, start_segs["month"], s_parts[1], "month")
        await page.wait_for_timeout(500)  # 월 변경 안정화 대기
        await set_date_segment(page, start_segs["day"], s_parts[2], "day")

        # ── 최종 검증 + 보정: UI 표시 텍스트 기반 ──
        for retry in range(3):
            await page.wait_for_timeout(500)
            try:
                picker = page.locator('div[class*="date-range-picker__Root"]').first
                raw = await picker.inner_text(timeout=2000)
                txt = raw.replace("\n", "").replace(" ", "")
                import re as _re
                dm = _re.search(r'(\d{4})\.(\d{1,2})\.(\d{1,2})\.?-(\d{4})\.(\d{1,2})\.(\d{1,2})', txt)
                if dm:
                    dy1, dm1, dd1, dy2, dm2, dd2 = [int(x) for x in dm.groups()]
                    print(f"   [검증] 표시: {dy1}.{dm1}.{dd1} ~ {dy2}.{dm2}.{dd2} / 목표: {s_parts[0]}.{s_parts[1]}.{s_parts[2]} ~ {e_parts[0]}.{e_parts[1]}.{e_parts[2]}")

                    all_ok = (dy1 == s_parts[0] and dm1 == s_parts[1] and dd1 == s_parts[2]
                              and dy2 == e_parts[0] and dm2 == e_parts[1] and dd2 == e_parts[2])
                    if all_ok:
                        break

                    # 표시된 일자가 목표보다 크면 ArrowDown, 작으면 ArrowUp
                    if dd1 != s_parts[2]:
                        diff = s_parts[2] - dd1
                        print(f"   [보정 {retry+1}] 시작일 {dd1} → {s_parts[2]} (diff={diff})")
                        await start_segs["day"].click()
                        await page.wait_for_timeout(200)
                        key = "ArrowUp" if diff > 0 else "ArrowDown"
                        for _ in range(abs(diff)):
                            await page.keyboard.press(key)
                            await page.wait_for_timeout(100)
                    if dd2 != e_parts[2]:
                        diff = e_parts[2] - dd2
                        print(f"   [보정 {retry+1}] 종료일 {dd2} → {e_parts[2]} (diff={diff})")
                        await end_segs["day"].click()
                        await page.wait_for_timeout(200)
                        key = "ArrowUp" if diff > 0 else "ArrowDown"
                        for _ in range(abs(diff)):
                            await page.keyboard.press(key)
                            await page.wait_for_timeout(100)
            except Exception as e:
                print(f"   [검증 오류] {e}")

        # 데이터 로딩 대기
        await page.wait_for_timeout(3000)

        # 설정 확인
        try:
            picker = page.locator('div[class*="date-range-picker__Root"]').first
            raw = await picker.inner_text(timeout=2000)
            txt = raw.replace("\n", "").replace(" ", "")
            print(f"   [확인] 설정된 기간: {txt}")
        except Exception:
            pass

        print(f"   [OK] 커스텀 날짜 설정 완료")
        return True

    print(f"   [FAIL] 지원하지 않는 기간 형식: {period}")
    return False


async def get_save_dir(page):
    """
    UI에서 현재 선택된 지역명과 날짜 범위를 읽어 저장 경로를 생성.
    예: shucle_data/검단신도시/20260218_20260224/
    """
    # 지역명 읽기
    region_name = "unknown"
    try:
        triggers = page.locator('button[data-slot="trigger"][aria-haspopup="dialog"]')
        cnt = await triggers.count()
        for idx in range(cnt):
            btn = triggers.nth(idx)
            txt = await btn.inner_text(timeout=1000)
            if "DRT" not in txt and "전체 유형" not in txt and txt.strip():
                # "서구/\n검단신도시" → "검단신도시"
                parts = txt.replace("\n", "/").split("/")
                region_name = parts[-1].strip()
                break
    except Exception:
        pass

    # 날짜 범위 읽기 (inner_text가 "2026\n.\n1\n.\n28\n.\n-\n2026\n.\n2\n.\n24\n." 형태로 반환됨)
    date_str = "unknown"
    try:
        picker = page.locator('div[class*="date-range-picker__Root"]').first
        raw = await picker.inner_text(timeout=2000)
        # 개행 제거 후 공백 정리
        txt = raw.replace("\n", "").replace(" ", "")
        # "1주4주12주2026.1.28.-2026.2.24." 형태에서 날짜 추출
        match = re.search(r'(\d{4})\.(\d{1,2})\.(\d{1,2})\.?-(\d{4})\.(\d{1,2})\.(\d{1,2})', txt)
        if match:
            y1, m1, d1, y2, m2, d2 = match.groups()
            date_str = f"{y1}{int(m1):02d}{int(d1):02d}_{y2}{int(m2):02d}{int(d2):02d}"
    except Exception:
        pass

    save_dir = os.path.join(BASE_DATA_DIR, region_name, date_str)
    return save_dir, region_name, date_str


async def wait_for_chart_data(all_responses, start_idx, tab_name, timeout=60, stable_secs=8):
    """
    차트 데이터 응답이 안정될 때까지 대기.
    - chart/data 또는 explore_json 응답이 더 이상 안 들어올 때까지 기다림
    - timeout: 최대 대기 시간 (초)
    - stable_secs: 새 응답이 없는 시간 (초) — 이만큼 지나면 완료 판단
    """
    import time
    start_time = time.time()
    last_count = len(all_responses)
    last_change = start_time

    while True:
        now = time.time()
        elapsed = now - start_time
        current_count = len(all_responses)

        if current_count > last_count:
            last_count = current_count
            last_change = now

        # 안정화: stable_secs 동안 새 응답 없음
        if now - last_change >= stable_secs:
            break
        # 타임아웃
        if elapsed >= timeout:
            break

        await asyncio.sleep(1)

    total = len(all_responses) - start_idx
    chart_data_count = sum(
        1 for r in all_responses[start_idx:]
        if "chart/data" in r["url"] or "explore_json" in r["url"]
    )
    return total, chart_data_count


async def get_zone_id_for_region(context, region_name):
    """zone API에서 선택된 지역의 zone_id를 조회"""
    # 방법 1: Playwright context request (쿠키 포함)
    try:
        resp = await context.request.get(
            "https://api-coco.shucle.com/v1/zone/list?target=real"
        )
        if resp.ok:
            data = await resp.json()
            zones = data if isinstance(data, list) else data.get("data", data.get("result", []))
            for zone in zones:
                if isinstance(zone, dict):
                    dn = zone.get("display_name", "")
                    if region_name == dn or region_name in dn or dn in region_name:
                        return str(zone.get("id", ""))
    except Exception:
        pass

    # 방법 2: urllib (인증 불필요 시 폴백)
    try:
        import urllib.request
        req = urllib.request.Request("https://api-coco.shucle.com/v1/zone/list?target=real")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        zones = data if isinstance(data, list) else data.get("data", data.get("result", []))
        for zone in zones:
            if isinstance(zone, dict):
                dn = zone.get("display_name", "")
                if region_name == dn or region_name in dn or dn in region_name:
                    return str(zone.get("id", ""))
    except Exception:
        pass

    return None


def verify_collected_zone(save_dir, expected_zone_id=None):
    """수집된 데이터 파일들의 zone_id를 검증하여 지역 일치 여부 확인"""
    zone_counts = {}   # zone_id -> 파일 수
    file_zones = {}    # filename -> set of zone_ids
    total_files = 0

    for fname in sorted(os.listdir(save_dir)):
        if not fname.endswith(".json") or fname.startswith("00") or fname.startswith("_"):
            continue
        total_files += 1
        fpath = os.path.join(save_dir, fname)
        with open(fpath, "r", encoding="utf-8") as f:
            text = f.read()

        # zone_id IN ('NNN') 패턴 추출 (Jinja 템플릿 {{ 제외)
        real_zones = re.findall(r"zone_id\s+IN\s*\('(\d+)'\)", text)
        if real_zones:
            file_zones[fname] = set(real_zones)
            for zid in set(real_zones):
                zone_counts[zid] = zone_counts.get(zid, 0) + 1

    if not zone_counts:
        return {
            "total_files": total_files,
            "files_with_zone": 0,
            "zone_counts": {},
            "primary_zone": None,
            "contaminated": [],
            "passed": None,
        }

    primary_zone = max(zone_counts, key=zone_counts.get)
    check_zone = str(expected_zone_id) if expected_zone_id else primary_zone

    contaminated = []
    for fname, zones in file_zones.items():
        bad_zones = zones - {check_zone}
        if bad_zones:
            contaminated.append((fname, bad_zones))

    passed = len(contaminated) == 0
    if expected_zone_id:
        passed = passed and (str(expected_zone_id) == primary_zone)

    return {
        "total_files": total_files,
        "files_with_zone": len(file_zones),
        "zone_counts": zone_counts,
        "primary_zone": primary_zone,
        "expected_zone": str(expected_zone_id) if expected_zone_id else None,
        "contaminated": contaminated,
        "passed": passed,
    }


async def main():
    print(f"[시작] 데이터 저장 기본 경로: {os.path.abspath(BASE_DATA_DIR)}\n")

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=BROWSER_PROFILE, headless=False,
            viewport={"width": 1920, "height": 1080}, slow_mo=300,
        )
        page = context.pages[0] if context.pages else await context.new_page()

        # ============================================================
        # 전역 API 응답 수집기
        # ============================================================
        all_responses = []       # 전체 API 응답
        current_tab = "초기로딩"  # 현재 탭 추적
        response_errors = []     # 응답 읽기 실패 로그

        async def on_response(response):
            url = response.url
            ct = response.headers.get("content-type", "")

            # JSON 응답 또는 API 패턴 매칭
            is_api = "json" in ct or any(pat in url.lower() for pat in API_PATTERNS)
            if not is_api:
                return

            # 응답 본문 즉시 읽기 (프레임 교체 전에 확보)
            body = None
            try:
                body = await response.body()
            except Exception as e:
                # 프레임이 교체/해제된 경우 body 읽기 실패
                response_errors.append({"tab": current_tab, "url": url[:120], "error": str(e)})
                return

            try:
                text = body.decode("utf-8", errors="replace")
                if len(text) < 10:
                    return

                # JSON 파싱 시도
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError:
                    parsed = None

                entry = {
                    "tab": current_tab,
                    "url": url,
                    "status": response.status,
                    "content_type": ct,
                    "body_size": len(text),
                    "body_raw": text[:5000],
                    "parsed": parsed,
                    "has_numeric_data": bool(re.search(r'\d{2,}', text[:2000])),
                }
                all_responses.append(entry)

            except Exception as e:
                response_errors.append({"tab": current_tab, "url": url[:120], "error": str(e)})

        page.on("response", on_response)

        # ============================================================
        # 사이트 접속
        # ============================================================
        print("[접속] 사이트 접속 중...")
        await page.goto(SITE_URL, wait_until="networkidle", timeout=60000)
        await page.wait_for_timeout(5000)

        # UI 요소 로딩 대기 (지역 드롭다운 버튼이 나타날 때까지)
        try:
            await page.wait_for_selector(
                'button[data-slot="trigger"][aria-haspopup="dialog"]',
                timeout=30000
            )
            await page.wait_for_timeout(2000)
        except Exception:
            print("[접속] 드롭다운 버튼 대기 타임아웃 — 추가 대기 10초")
            await page.wait_for_timeout(10000)

        # ============================================================
        # 로그인 대기 (수동)
        # ============================================================
        if "login" in page.url.lower() or "auth" in page.url.lower():
            print(f"[로그인] 브라우저에서 로그인해주세요 (최대 {LOGIN_TIMEOUT}초 대기)")
            try:
                await page.wait_for_url("**/metrics**", timeout=LOGIN_TIMEOUT * 1000)
                await page.wait_for_timeout(5000)
                print("[로그인] 로그인 완료!")
            except Exception:
                print("[로그인] 시간 초과 — 종료합니다")
                await context.close()
                return
        else:
            print("[로그인] 이미 로그인 상태")

        # ============================================================
        # 지역 선택 (자동 재시도 + 수동 대기 폴백)
        # ============================================================
        region_ok = False
        for attempt in range(1, REGION_RETRY + 1):
            print(f"\n[지역] 선택 시도 {attempt}/{REGION_RETRY}")
            if await select_region(page, REGION_KEYWORD):
                region_ok = True
                break
            await page.wait_for_timeout(2000)

        if not region_ok:
            print(f"\n[지역] 자동 선택 실패 -> 수동 선택 대기 ({REGION_MANUAL_WAIT}초)")
            print(f"[지역] 브라우저에서 직접 '{REGION_KEYWORD}'를 선택해주세요!")
            for sec in range(REGION_MANUAL_WAIT):
                await page.wait_for_timeout(1000)
                try:
                    body_text = await page.locator("body").inner_text(timeout=1000)
                    if REGION_KEYWORD in body_text:
                        print(f"   [OK] {REGION_KEYWORD} 감지됨 ({sec+1}초 경과)")
                        region_ok = True
                        break
                except Exception:
                    pass
                if (sec + 1) % 10 == 0:
                    print(f"   ... 대기 중 ({sec+1}/{REGION_MANUAL_WAIT}초)")

            if not region_ok:
                print(f"[지역] {REGION_KEYWORD} 선택 확인 불가 — 현재 상태로 계속 진행합니다")

        # ============================================================
        # 기간 설정
        # ============================================================
        await select_date_range(page, DATE_RANGE)

        # ============================================================
        # 지역/기간 설정 전 응답 제거 (이전 지역 데이터 오염 방지)
        # ============================================================
        old_count = len(all_responses)
        all_responses.clear()
        response_errors.clear()
        if old_count:
            print(f"\n[정리] 지역/기간 설정 전 응답 {old_count}개 제거 (이전 지역 데이터 오염 방지)")

        # 지역/기간 변경 후 차트 재로딩 대기
        await page.wait_for_timeout(5000)

        # ============================================================
        # 초기로딩 완료 대기 (호출 탑승 탭 차트 데이터)
        # ============================================================
        print("\n[초기] 초기 차트 데이터 로딩 대기 중...")
        init_idx = 0
        init_total, init_charts = await wait_for_chart_data(
            all_responses, init_idx, "초기로딩", timeout=60, stable_secs=10
        )
        print(f"   [OK] 초기로딩: 전체 {init_total}개 (차트 {init_charts}개)")

        # ============================================================
        # 저장 경로 결정 (지역/날짜 기반)
        # ============================================================
        save_dir, region_name, date_str = await get_save_dir(page)

        # 기존 데이터 정리 (이전 실행 잔여 파일 제거)
        if os.path.exists(save_dir):
            old_files = [f for f in os.listdir(save_dir)
                        if f.endswith(".json") and not f.startswith("00")]
            if old_files:
                for f in old_files:
                    os.remove(os.path.join(save_dir, f))
                print(f"\n[정리] 기존 데이터 파일 {len(old_files)}개 삭제 (재수집)")

        os.makedirs(save_dir, exist_ok=True)
        print(f"\n[저장] 경로: {os.path.abspath(save_dir)}")
        print(f"   지역: {region_name} / 기간: {date_str}")

        # 선택 확인 스크린샷
        confirm_path = os.path.join(save_dir, "00_region_confirm.png")
        await page.screenshot(path=confirm_path)
        print(f"   [스크린샷] {confirm_path}")

        # ============================================================
        # 각 탭 순회하며 API 응답 수집
        # ============================================================
        print(f"\n{'='*60}")
        print(f"[탐색] 탭별 API 응답 탐색 시작")
        print(f"{'='*60}")

        for i, tab_name in enumerate(TABS, 1):
            current_tab = tab_name
            tab_start_idx = len(all_responses)

            print(f"\n[{i}/{len(TABS)}] {tab_name}")
            print("-" * 40)

            try:
                tab_el = page.get_by_text(tab_name, exact=True).first
                await tab_el.click()

                # 초기 로딩 대기 (iframe 생성 + guest_token + 대시보드 메타)
                await page.wait_for_timeout(10000)

                # 스크롤로 lazy-load 트리거 (하단까지 충분히 스크롤)
                for scroll_i in range(5):
                    await page.mouse.wheel(0, 800)
                    await page.wait_for_timeout(1500)
                # 상단으로 복귀 후 다시 하단으로 (2차 lazy-load)
                await page.mouse.wheel(0, -5000)
                await page.wait_for_timeout(2000)
                for scroll_i in range(5):
                    await page.mouse.wheel(0, 800)
                    await page.wait_for_timeout(1500)

                # 차트 데이터 응답 안정화 대기 (최대 60초, 8초간 새 응답 없으면 완료)
                tab_total, tab_charts = await wait_for_chart_data(
                    all_responses, tab_start_idx, tab_name, timeout=60, stable_secs=8
                )

                # 차트 데이터가 0개면 추가 스크롤 + 재대기
                if tab_charts == 0:
                    print(f"   [재시도] 차트 데이터 0개 — 추가 대기...")
                    await page.mouse.wheel(0, -5000)
                    await page.wait_for_timeout(3000)
                    for _ in range(7):
                        await page.mouse.wheel(0, 800)
                        await page.wait_for_timeout(2000)

                    tab_total2, tab_charts2 = await wait_for_chart_data(
                        all_responses, tab_start_idx, tab_name, timeout=30, stable_secs=8
                    )
                    tab_total = tab_total2
                    tab_charts = tab_charts2

                print(f"   -> 전체 {tab_total}개 (차트 데이터 {tab_charts}개)")

            except Exception as e:
                print(f"   [FAIL] 탭 전환 실패: {e}")

        # ============================================================
        # 차트 수집 검증 및 누락 차트 재수집
        # ============================================================

        # slice_id → slice_name 매핑 구축 (dashboard/charts 응답에서 추출)
        slice_map = {}
        # slice_id → dashboard_id 매핑
        slice_dashboard = {}
        for r in all_responses:
            parsed = r.get("parsed")
            if not isinstance(parsed, dict):
                continue
            result = parsed.get("result")
            if not isinstance(result, list):
                continue
            # dashboard/charts 응답에서 차트 목록 추출
            has_slice = any(isinstance(x, dict) and "slice_name" in x for x in result[:3])
            if not has_slice:
                continue
            # URL에서 dashboard_id 추출
            m = re.search(r"dashboard/(\d+)", r["url"])
            did = int(m.group(1)) if m else None
            for item in result:
                if isinstance(item, dict) and "slice_name" in item and "id" in item:
                    slice_map[item["id"]] = item["slice_name"]
                    if did:
                        slice_dashboard[item["id"]] = did

        # 수집된 차트 slice_id 세트
        collected_sids = set()
        for r in all_responses:
            url = r["url"]
            if "chart/data" not in url and "explore_json" not in url:
                continue
            # URL 인코딩 형태: slice_id%22%3A1234 또는 slice_id":1234
            m = re.search(r"slice_id(?:%22%3A|[\":\s]+)(\d+)", url)
            if m:
                collected_sids.add(int(m.group(1)))

        # 수집 불가능한 차트 (지도 히트맵 등 chart/data API 미사용)
        SKIP_SIDS = {3756, 3757}  # 정류장 이용 지도 히트맵

        # 누락 차트 식별
        expected_sids = set(slice_map.keys())
        missing_sids = expected_sids - collected_sids - SKIP_SIDS

        print(f"\n{'='*60}")
        print(f"[검증] 차트 수집 완전성 검증")
        print(f"{'='*60}")
        print(f"   기대 차트: {len(expected_sids)}개 (지도 {len(SKIP_SIDS)}개 제외)")
        print(f"   수집 완료: {len(collected_sids)}개")
        print(f"   미수집: {len(missing_sids)}개")

        # 누락 차트가 있으면 iframe fetch로 직접 재수집
        if missing_sids:
            print(f"\n[재수집] 누락 {len(missing_sids)}개 차트 직접 API 호출...")
            for sid in sorted(missing_sids):
                sname = slice_map.get(sid, f"slice_{sid}")
                print(f"   누락: {sid} = {sname}")

            # Superset iframe 찾기
            superset_frame = None
            for frame in page.frames:
                if "superset" in frame.url:
                    superset_frame = frame
                    break

            if superset_frame:
                retry_ok = 0
                retry_fail = 0
                for sid in sorted(missing_sids):
                    did = slice_dashboard.get(sid)
                    sname = slice_map.get(sid, f"slice_{sid}")
                    if not did:
                        print(f"   [SKIP] {sid}: dashboard_id 없음")
                        retry_fail += 1
                        continue
                    api_path = f"/api/v1/chart/data?form_data=%7B%22slice_id%22%3A{sid}%7D&dashboard_id={did}"
                    try:
                        result_text = await superset_frame.evaluate(
                            """async (apiPath) => {
                                const resp = await fetch(apiPath);
                                return await resp.text();
                            }""",
                            api_path,
                        )
                        if result_text and len(result_text) > 100:
                            try:
                                parsed_data = json.loads(result_text)
                            except json.JSONDecodeError:
                                parsed_data = None

                            all_responses.append({
                                "tab": "재수집",
                                "url": f"https://superset1.shucle.com{api_path}",
                                "status": 200,
                                "content_type": "application/json",
                                "body_size": len(result_text),
                                "body_raw": result_text[:5000],
                                "parsed": parsed_data,
                                "has_numeric_data": True,
                            })
                            collected_sids.add(sid)
                            retry_ok += 1
                            print(f"   [OK] {sid}: {sname}")
                        else:
                            retry_fail += 1
                            print(f"   [FAIL] {sid}: {sname} (빈 응답)")
                    except Exception as e:
                        retry_fail += 1
                        print(f"   [FAIL] {sid}: {sname} ({str(e)[:60]})")
                    # API 과부하 방지
                    await page.wait_for_timeout(500)

                still_missing = expected_sids - collected_sids - SKIP_SIDS
                print(f"\n   재수집 결과: 성공={retry_ok}, 실패={retry_fail}")
                if still_missing:
                    print(f"   [경고] 여전히 미수집: {len(still_missing)}개")
                    for sid in sorted(still_missing):
                        print(f"      {sid}: {slice_map.get(sid, '?')}")
                else:
                    print(f"   [OK] 모든 차트 수집 완료!")
            else:
                print(f"   [FAIL] Superset iframe을 찾을 수 없어 재수집 불가")
        else:
            print(f"   [OK] 모든 차트 수집 완료!")

        # 최종 수집률
        final_collected = len(collected_sids)
        final_expected = len(expected_sids - SKIP_SIDS)
        pct = final_collected / final_expected * 100 if final_expected else 0
        print(f"   최종 수집률: {final_collected}/{final_expected} ({pct:.1f}%)")

        # ============================================================
        # 결과 분석 및 저장
        # ============================================================
        print(f"\n\n{'='*60}")
        print(f"[결과] 수집 결과 분석")
        print(f"{'='*60}")
        print(f"총 API 응답: {len(all_responses)}개")
        if response_errors:
            print(f"응답 읽기 실패: {len(response_errors)}개")
        print()

        print(f"차트 이름 매핑: {len(slice_map)}개 slice_id 확인")

        # 탭별 요약
        tab_summary = {}
        for r in all_responses:
            tab = r["tab"]
            tab_summary.setdefault(tab, []).append(r)

        for tab, responses in tab_summary.items():
            chart_count = sum(1 for r in responses if "chart/data" in r["url"] or "explore_json" in r["url"])
            print(f"\n[{tab}] -- {len(responses)}개 API (차트 {chart_count}개)")
            for j, r in enumerate(responses):
                url_short = r["url"][:100]
                has_data = "[DATA]" if r["has_numeric_data"] else "      "
                print(f"  {has_data} [{r['status']}] {r['body_size']:>6}B | {url_short}")

                # 숫자 데이터가 있는 큰 응답은 별도 파일로 저장
                if r["has_numeric_data"] and r["body_size"] > 100:
                    fname = f"{tab.replace(' ', '_')}_{j:02d}.json"
                    fpath = os.path.join(save_dir, fname)

                    # URL에서 slice_id 추출 → slice_name 매핑
                    slice_id = None
                    slice_name = None
                    try:
                        parsed_url = urllib.parse.urlparse(r["url"])
                        params = urllib.parse.parse_qs(parsed_url.query)
                        if "form_data" in params:
                            fd = json.loads(params["form_data"][0])
                            slice_id = fd.get("slice_id")
                            if slice_id and slice_id in slice_map:
                                slice_name = slice_map[slice_id]
                    except Exception:
                        pass

                    with open(fpath, "w", encoding="utf-8") as f:
                        save_data = r["parsed"] if r["parsed"] else r["body_raw"]
                        # 차트 데이터 JSON이면 메타데이터 래핑
                        if isinstance(save_data, dict) and slice_id:
                            save_data["_meta"] = {
                                "slice_id": slice_id,
                                "slice_name": slice_name,
                                "tab": tab,
                                "url": r["url"][:300],
                            }
                            json.dump(save_data, f, ensure_ascii=False, indent=2)
                        elif isinstance(save_data, dict):
                            json.dump(save_data, f, ensure_ascii=False, indent=2)
                        else:
                            f.write(save_data)

        # 전체 응답 요약 저장
        summary = []
        for r in all_responses:
            entry = {
                "tab": r["tab"],
                "url": r["url"][:200],
                "status": r["status"],
                "size": r["body_size"],
                "has_data": r["has_numeric_data"],
                "content_type": r["content_type"],
                "body_preview": r["body_raw"][:500],
            }
            # slice_id/slice_name 추가
            try:
                parsed_url = urllib.parse.urlparse(r["url"])
                params = urllib.parse.parse_qs(parsed_url.query)
                if "form_data" in params:
                    fd = json.loads(params["form_data"][0])
                    sid = fd.get("slice_id")
                    if sid:
                        entry["slice_id"] = sid
                        entry["slice_name"] = slice_map.get(sid)
            except Exception:
                pass
            summary.append(entry)

        summary_path = os.path.join(save_dir, "_summary.json")
        with open(summary_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)

        # 응답 실패 로그 저장
        if response_errors:
            err_path = os.path.join(save_dir, "_response_errors.json")
            with open(err_path, "w", encoding="utf-8") as f:
                json.dump(response_errors, f, ensure_ascii=False, indent=2)
            print(f"\n[경고] 응답 읽기 실패 {len(response_errors)}건 -> {err_path}")

        # iframe 확인
        print(f"\n\n[프레임] 프레임 정보:")
        for i, frame in enumerate(page.frames):
            url = frame.url[:120] if frame.url else "(없음)"
            print(f"  [{i}] {frame.name or '(메인)'} -> {url}")

        # 쿠키/토큰 정보 (Guest token 등)
        cookies = await context.cookies()
        token_cookies = [c for c in cookies if any(k in c["name"].lower() for k in ["token", "auth", "session", "guest"])]
        if token_cookies:
            print(f"\n[인증] 인증 관련 쿠키:")
            for c in token_cookies:
                print(f"  {c['name']}: {c['value'][:50]}...")

        # 탭별 차트 데이터 수집 요약
        print(f"\n\n{'='*60}")
        print(f"[요약] 탭별 차트 데이터 수집 결과")
        print(f"{'='*60}")
        for tab, responses in tab_summary.items():
            chart_count = sum(1 for r in responses if "chart/data" in r["url"] or "explore_json" in r["url"])
            status = "[OK]" if chart_count > 0 else "[MISS]"
            print(f"  {status} {tab}: API {len(responses)}개 / 차트 {chart_count}개")

        print(f"\n{'='*60}")
        print(f"[완료] 결과 파일: {os.path.abspath(save_dir)}")
        print(f"{'='*60}")

        # ============================================================
        # 수집 데이터 지역(zone_id) 검증
        # ============================================================
        print(f"\n{'='*60}")
        print(f"[검증] 수집 데이터 지역(zone_id) 검증")
        print(f"{'='*60}")

        expected_zone_id = await get_zone_id_for_region(context, region_name)
        if expected_zone_id:
            print(f"   zone API 조회: {region_name} -> zone_id={expected_zone_id}")
        else:
            print(f"   zone API 조회 실패 -- 수집 데이터 내 다수결로 판단")

        vr = verify_collected_zone(save_dir, expected_zone_id)

        print(f"   검사 파일: {vr['total_files']}개 (zone_id 포함: {vr['files_with_zone']}개)")
        if vr["zone_counts"]:
            zone_dist = ", ".join(f"zone_id={k}: {v}개" for k, v in sorted(vr["zone_counts"].items(), key=lambda x: -x[1]))
            print(f"   zone_id 분포: {zone_dist}")

        if vr["passed"] is True:
            zid = vr["expected_zone"] or vr["primary_zone"]
            print(f"   [PASS] 모든 데이터가 {region_name}(zone_id={zid})과 일치합니다")
        elif vr["passed"] is False:
            print(f"   [FAIL] 지역 불일치 감지!")
            if vr["expected_zone"] and vr["expected_zone"] != vr["primary_zone"]:
                print(f"          예상 zone_id={vr['expected_zone']}, 실제 주요 zone_id={vr['primary_zone']}")
            for fname, bad_zones in vr["contaminated"]:
                print(f"          오염 파일: {fname} -> zone_id={bad_zones}")
        else:
            print(f"   [SKIP] zone_id 필터가 포함된 파일 없음 -- 검증 불가")

        # 검증 결과 저장
        verify_path = os.path.join(save_dir, "_zone_verify.json")
        with open(verify_path, "w", encoding="utf-8") as f:
            json.dump({
                "region_name": region_name,
                "region_keyword": REGION_KEYWORD,
                "expected_zone_id": expected_zone_id,
                "primary_zone_id": vr["primary_zone"],
                "zone_counts": vr["zone_counts"],
                "total_files": vr["total_files"],
                "files_with_zone": vr["files_with_zone"],
                "contaminated_count": len(vr["contaminated"]),
                "contaminated_files": [
                    {"file": fname, "zone_ids": list(zones)}
                    for fname, zones in vr["contaminated"]
                ],
                "passed": vr["passed"],
            }, f, ensure_ascii=False, indent=2)
        print(f"   검증 결과 저장: {verify_path}")

        # 자동 종료 (10초 후)
        print(f"\n[종료] 10초 후 브라우저를 닫습니다...")
        await page.wait_for_timeout(10000)
        await context.close()
        print("[종료] 완료!")


if __name__ == "__main__":
    print("=" * 60)
    print("[시작] 셔클 인사이트 API 탐색 (Step 1)")
    print(f"[시간] {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 60)
    prompt_settings()
    asyncio.run(main())
