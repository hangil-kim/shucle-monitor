"""배치 데이터 수집 스크립트 (고속 버전)
================================
여러 지역/기간 조합을 단일 브라우저 세션에서 순차 수집합니다.
빠르게 수집 후 검증 → 누락 차트만 API로 재수집하는 방식으로 시간을 단축합니다.

실행: python batch_collect.py
"""

import asyncio, os, json, re, sys, urllib.parse, time
from datetime import datetime
from playwright.async_api import async_playwright

sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from shucle_api_probe import (
    SITE_URL, BROWSER_PROFILE, BASE_DATA_DIR, TABS, API_PATTERNS,
    LOGIN_TIMEOUT, REGION_RETRY,
    select_region, select_date_range, get_save_dir,
    get_zone_id_for_region, verify_collected_zone,
)

# ================================================================
# 배치 수집 설정
# ================================================================
JOBS = [
    # 재수집: 80% 미달 지역, 부족한 탭만 선택 (merge 모드)
    ("동면",     ("2026-04-13", "2026-04-19"), ["호출 탑승", "서비스 품질", "지역 회원", "정류장 이용", "차량 운행"]),
    ("삼호",     ("2026-04-13", "2026-04-19"), ["호출 탑승", "서비스 품질", "차량 운행"]),
    ("봉양",     ("2026-04-13", "2026-04-19"), ["호출 탑승", "서비스 품질"]),
    ("백운",     ("2026-04-13", "2026-04-19"), ["호출 탑승", "서비스 품질", "차량 운행"]),
    ("해안",     ("2026-04-13", "2026-04-19"), ["호출 탑승", "서비스 품질"]),
    ("영덕",     ("2026-04-13", "2026-04-19"), ["호출 탑승", "서비스 품질"]),
    ("충북혁신", ("2026-04-13", "2026-04-19"), ["호출 탑승", "서비스 품질"]),
]

# 지역 키워드 → 예상 display_name 매핑 (리포트 생성 시 사용)
REGION_MAP = {
    "백운": "백운면",
    "봉양": "봉양읍",
    "검단": "검단신도시",
    "충북혁신": "충북혁신도시",
    "삼호": "삼호",
    "동면": "동면",
    "해안": "해안면",
    "영덕": "영덕관광",
}

# ================================================================
# 고속 수집 설정 (기존 대비 ~60% 시간 단축)
# ================================================================
FAST = {
    "slow_mo": 50,           # 기존 300 → 50 (동작 간 지연)
    "tab_init_wait": 5000,   # 탭 클릭 후 대기 (3000→5000, 차트 로딩 충분히)
    "scroll_wait": 800,      # 스크롤 간 대기 (500→800, lazy-load 여유)
    "scroll_top_wait": 1500, # 상단 복귀 대기 (1000→1500)
    "stable_secs": 6,        # 응답 안정화 판단 (4→6초)
    "init_stable": 8,        # 초기 로딩 안정화 (5→8초)
    "chart_timeout": 45,     # 차트 대기 최대 (30→45초)
    "region_change_wait": 5000,  # 지역 변경 후 (3000→5000)
    "retry_wait": 500,       # 재수집 API 간 (300→500)
    "next_job_wait": 2000,   # 다음 작업 전 (1000→2000)
}

REPORT_THRESHOLD = 80.0   # 리포트 생성 기준 수집률 (%)
MAX_RETRY_ROUNDS = 5       # 최대 재시도 라운드 수

# 80% 미달 시 재시도할 탭 목록 (지역명 기준)
RETRY_TABS = {
    "동면":         ["호출 탑승", "서비스 품질", "지역 회원", "정류장 이용", "차량 운행"],
    "삼호":         ["호출 탑승", "서비스 품질", "차량 운행"],
    "봉양읍":       ["호출 탑승", "서비스 품질"],
    "백운면":       ["호출 탑승", "서비스 품질", "차량 운행"],
    "해안면":       ["호출 탑승", "서비스 품질"],
    "영덕관광":     ["호출 탑승", "서비스 품질"],
    "충북혁신도시": ["호출 탑승", "서비스 품질"],
}


def get_true_rate(region_name, date_str, expected=128):
    """_summary.json의 고유 slice_id 수로 실제 합산 수집률(%) 반환"""
    sm = os.path.join(BASE_DATA_DIR, region_name, date_str, "_summary.json")
    if not os.path.exists(sm):
        return 0.0
    with open(sm, "r", encoding="utf-8") as f:
        s = json.load(f)
    unique_sids = set(e["slice_id"] for e in s if e.get("slice_id"))
    return len(unique_sids) / expected * 100


async def wait_for_responses(all_responses, start_idx, timeout=30, stable_secs=4):
    """응답이 안정될 때까지 대기 (경량 버전)"""
    start_time = time.time()
    last_count = len(all_responses)
    last_change = start_time

    while True:
        now = time.time()
        current_count = len(all_responses)
        if current_count > last_count:
            last_count = current_count
            last_change = now
        if now - last_change >= stable_secs:
            break
        if now - start_time >= timeout:
            break
        await asyncio.sleep(0.5)

    total = len(all_responses) - start_idx
    chart_count = sum(
        1 for r in all_responses[start_idx:]
        if "chart/data" in r["url"] or "explore_json" in r["url"]
    )
    return total, chart_count


async def collect_one(page, context, all_responses, response_errors, region_keyword, period, target_tabs=None):
    """단일 지역/기간 데이터 수집 (고속 버전)
    target_tabs: None이면 전체 탭 수집, 리스트면 해당 탭만 수집 (merge 모드)
    """
    merge_mode = target_tabs is not None
    job_start = time.time()
    print(f"\n{'='*60}")
    print(f"[수집] 지역={region_keyword}, 기간={period}")
    print(f"{'='*60}")

    # ── 모달/팝업 닫기 (이전 작업에서 열린 대화상자 차단 방지) ──
    for _ in range(3):
        try:
            overlay = page.locator('div[data-slot="wrapper"][class*="fixed inset-0"]')
            if await overlay.count() > 0:
                print("[정리] 모달/오버레이 감지 — Escape로 닫기")
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(1000)
            else:
                break
        except Exception:
            break

    # ── 지역 선택 ──
    region_ok = False
    for attempt in range(1, REGION_RETRY + 1):
        if await select_region(page, region_keyword):
            region_ok = True
            break
        await page.wait_for_timeout(1000)

    if not region_ok:
        print(f"[지역] 자동 선택 실패 — 20초 수동 대기")
        for sec in range(20):
            await page.wait_for_timeout(1000)
            try:
                body_text = await page.locator("body").inner_text(timeout=1000)
                if region_keyword in body_text:
                    region_ok = True
                    break
            except Exception:
                pass

    # ── 기간 설정 ──
    await select_date_range(page, period)

    # ── 호출 탑승 리로드 대응 ──
    # 호출 탑승은 기본 탭이라 date 변경 시 이미 로딩 → clear로 유실
    # 다른 탭으로 이동하여 호출 탑승 데이터를 언로드시킨 뒤, 나중에 돌아와서 수집
    tabs_need_call = target_tabs is None or "호출 탑승" in target_tabs
    if tabs_need_call:
        try:
            # 차량 운행(마지막 탭)으로 이동 → 호출 탑승 차트 완전 언로드
            far_tab = page.get_by_text("차량 운행", exact=True).first
            await far_tab.click()
            await page.wait_for_timeout(3000)
            # 스크롤로 차트 로딩 트리거 (기존 캐시 무효화)
            for _ in range(3):
                await page.mouse.wheel(0, 800)
                await page.wait_for_timeout(500)
            await page.mouse.wheel(0, -5000)
            await page.wait_for_timeout(2000)
            print("[초기] 호출 탑승 리로드 대응: 차량 운행 탭으로 이동 후 복귀 준비")
        except Exception:
            pass

    # ── 이전 응답 정리 (clear 시점을 탭 이동 후로) ──
    old_count = len(all_responses)
    all_responses.clear()
    response_errors.clear()
    if old_count:
        print(f"[정리] 이전 응답 {old_count}개 제거")

    await page.wait_for_timeout(FAST["region_change_wait"])

    # ── 초기 로딩 대기 ──
    print("[초기] 차트 데이터 로딩 대기...")
    init_total, init_charts = await wait_for_responses(
        all_responses, 0, timeout=FAST["chart_timeout"], stable_secs=FAST["init_stable"]
    )
    print(f"   초기로딩: {init_total}개 (차트 {init_charts}개)")

    # ── 저장 경로 ──
    save_dir, region_name, date_str = await get_save_dir(page)
    os.makedirs(save_dir, exist_ok=True)
    print(f"[저장] {region_name}/{date_str}")

    await page.screenshot(path=os.path.join(save_dir, "00_region_confirm.png"))

    # ── 수집 대상 탭 결정 ──
    tabs_to_visit = target_tabs if target_tabs else TABS
    print(f"\n[탐색] {len(tabs_to_visit)}개 탭 {'보충' if merge_mode else '고속'} 순회")

    total_tabs = len(tabs_to_visit)
    for i, tab_name in enumerate(tabs_to_visit, 1):
        tab_start_idx = len(all_responses)

        try:
            tab_el = page.get_by_text(tab_name, exact=True).first
            await tab_el.click()
            await page.wait_for_timeout(FAST["tab_init_wait"])

            # 1회 스크롤 (빠르게)
            for _ in range(5):
                await page.mouse.wheel(0, 800)
                await page.wait_for_timeout(FAST["scroll_wait"])
            await page.mouse.wheel(0, -5000)
            await page.wait_for_timeout(FAST["scroll_top_wait"])
            for _ in range(5):
                await page.mouse.wheel(0, 800)
                await page.wait_for_timeout(FAST["scroll_wait"])

            tab_total, tab_charts = await wait_for_responses(
                all_responses, tab_start_idx,
                timeout=FAST["chart_timeout"], stable_secs=FAST["stable_secs"]
            )

            # 차트 0건이면 추가 스크롤+재대기 (lazy-load 미트리거 대응)
            if tab_charts == 0:
                print(f"  [{i}/{total_tabs}] {tab_name}: 0개 — 추가 스크롤 재시도...")
                await page.mouse.wheel(0, -5000)
                await page.wait_for_timeout(3000)
                for _ in range(7):
                    await page.mouse.wheel(0, 800)
                    await page.wait_for_timeout(1000)
                await page.mouse.wheel(0, -5000)
                await page.wait_for_timeout(2000)
                for _ in range(7):
                    await page.mouse.wheel(0, 800)
                    await page.wait_for_timeout(1000)
                tab_total, tab_charts = await wait_for_responses(
                    all_responses, tab_start_idx,
                    timeout=FAST["chart_timeout"], stable_secs=FAST["stable_secs"]
                )

            print(f"  [{i}/{total_tabs}] {tab_name}: {tab_charts}개 차트")

        except Exception as e:
            print(f"  [{i}/{total_tabs}] {tab_name}: FAIL ({e})")

    # ── slice_map 구축 ──
    slice_map = {}
    slice_dashboard = {}
    for r in all_responses:
        parsed = r.get("parsed")
        if not isinstance(parsed, dict):
            continue
        result = parsed.get("result")
        if not isinstance(result, list):
            continue
        has_slice = any(isinstance(x, dict) and "slice_name" in x for x in result[:3])
        if not has_slice:
            continue
        m = re.search(r"dashboard/(\d+)", r["url"])
        did = int(m.group(1)) if m else None
        for item in result:
            if isinstance(item, dict) and "slice_name" in item and "id" in item:
                slice_map[item["id"]] = item["slice_name"]
                if did:
                    slice_dashboard[item["id"]] = did

    # ── 수집 검증 (빈 응답도 누락 판정) ──
    collected_sids = set()
    empty_sids = set()  # result가 빈 배열인 차트
    for r in all_responses:
        url = r["url"]
        if "chart/data" not in url and "explore_json" not in url:
            continue
        m = re.search(r"slice_id(?:%22%3A|[\":\s]+)(\d+)", url)
        if not m:
            continue
        sid = int(m.group(1))
        collected_sids.add(sid)
        # 빈 응답 검증: result가 빈 배열이면 실질적 누락
        parsed = r.get("parsed")
        if isinstance(parsed, dict):
            result = parsed.get("result", [])
            if isinstance(result, list):
                has_data = any(
                    isinstance(item, dict) and item.get("data") and len(item["data"]) > 0
                    for item in result
                )
                if not has_data:
                    empty_sids.add(sid)

    SKIP_SIDS = {3756, 3757}
    expected_sids = set(slice_map.keys())
    missing_sids = expected_sids - collected_sids - SKIP_SIDS
    # 빈 응답도 재수집 대상에 포함
    retry_sids = missing_sids | (empty_sids - SKIP_SIDS)
    final_expected = len(expected_sids - SKIP_SIDS)

    print(f"\n[검증] 수집 {len(collected_sids)}/{final_expected}, 누락 {len(missing_sids)}개, 빈응답 {len(empty_sids)}개")
    if empty_sids:
        empty_names = [slice_map.get(sid, f"#{sid}") for sid in sorted(empty_sids)]
        print(f"   빈응답 차트: {', '.join(empty_names[:10])}")

    # ── 누락+빈응답 차트 API 재수집 (최대 3라운드) ──
    if retry_sids:
        superset_frame = None
        for frame in page.frames:
            if "superset" in frame.url:
                superset_frame = frame
                break

        if superset_frame:
            for round_num in range(3):
                if not retry_sids:
                    break
                retry_ok = 0
                print(f"[재수집 R{round_num+1}] {len(retry_sids)}개 차트 API 호출...")
                for sid in sorted(retry_sids):
                    did = slice_dashboard.get(sid)
                    if not did:
                        continue
                    api_path = f"/api/v1/chart/data?form_data=%7B%22slice_id%22%3A{sid}%7D&dashboard_id={did}"
                    try:
                        result_text = await superset_frame.evaluate(
                            """async (apiPath) => {
                                const resp = await fetch(apiPath);
                                if (!resp.ok) return null;
                                return await resp.text();
                            }""",
                            api_path,
                        )
                        if result_text and len(result_text) > 100:
                            try:
                                parsed_data = json.loads(result_text)
                            except json.JSONDecodeError:
                                parsed_data = None
                            # 빈 응답인지 확인
                            has_real_data = False
                            if isinstance(parsed_data, dict):
                                for item in parsed_data.get("result", []):
                                    if isinstance(item, dict) and item.get("data") and len(item["data"]) > 0:
                                        has_real_data = True
                                        break
                            if has_real_data:
                                # 기존 빈 응답이 있으면 제거 (교체)
                                if sid in empty_sids:
                                    all_responses[:] = [
                                        r for r in all_responses
                                        if not (f"slice_id%22%3A{sid}" in r.get("url","") or f'"slice_id":{sid}' in r.get("url",""))
                                        or r.get("tab") == "재수집"  # 새로 추가된 건 유지
                                    ]
                                    empty_sids.discard(sid)
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
                    except Exception:
                        pass
                    await page.wait_for_timeout(FAST["retry_wait"])

                # 재검증
                retry_sids = (expected_sids - collected_sids - SKIP_SIDS) | (empty_sids - SKIP_SIDS)
                print(f"   성공 {retry_ok}개, 잔여 {len(retry_sids)}개 (누락 {len(expected_sids - collected_sids - SKIP_SIDS)}, 빈응답 {len(empty_sids)})")

    final_collected = len(collected_sids)
    pct = final_collected / final_expected * 100 if final_expected else 0
    print(f"   최종: {final_collected}/{final_expected} ({pct:.1f}%)")

    # ── 저장 ──
    if not merge_mode:
        old_files = [f for f in os.listdir(save_dir)
                     if f.endswith(".json") and not f.startswith("00")]
        if old_files:
            for f in old_files:
                os.remove(os.path.join(save_dir, f))
    else:
        print(f"[merge] 기존 데이터 유지, 보충분만 추가")

    tab_summary = {}
    for r in all_responses:
        tab_summary.setdefault(r["tab"], []).append(r)

    merge_prefix = "수집_" if not merge_mode else "보충_"
    for tab, responses in tab_summary.items():
        for j, r in enumerate(responses):
            if r["has_numeric_data"] and r["body_size"] > 100:
                fname = f"{merge_prefix}{tab.replace(' ', '_')}_{j:02d}.json"
                fpath = os.path.join(save_dir, fname)
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

    # 요약 저장
    summary = []
    for r in all_responses:
        entry = {
            "tab": r["tab"], "url": r["url"][:200], "status": r["status"],
            "size": r["body_size"], "has_data": r["has_numeric_data"],
            "content_type": r["content_type"], "body_preview": r["body_raw"][:500],
        }
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
    if merge_mode and os.path.exists(summary_path):
        # 기존 summary에 보충분 병합
        with open(summary_path, "r", encoding="utf-8") as f:
            existing_summary = json.load(f)
        # 기존 slice_id 목록
        existing_sids = set()
        for e in existing_summary:
            if "slice_id" in e:
                existing_sids.add(e["slice_id"])
        # 새로 수집된 것 중 기존에 없는 것만 추가
        added = 0
        for s in summary:
            sid = s.get("slice_id")
            if sid and sid not in existing_sids:
                existing_summary.append(s)
                existing_sids.add(sid)
                added += 1
            elif not sid:
                existing_summary.append(s)
                added += 1
        print(f"[merge] _summary.json: 기존 {len(existing_summary)-added}개 + 보충 {added}개")
        summary = existing_summary
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    # zone 검증
    expected_zone_id = await get_zone_id_for_region(context, region_name)
    vr = verify_collected_zone(save_dir, expected_zone_id)
    zone_str = "PASS" if vr["passed"] else ("FAIL" if vr["passed"] is False else "SKIP")

    with open(os.path.join(save_dir, "_zone_verify.json"), "w", encoding="utf-8") as f:
        json.dump({
            "region_name": region_name, "region_keyword": region_keyword,
            "expected_zone_id": expected_zone_id,
            "primary_zone_id": vr["primary_zone"],
            "zone_counts": vr["zone_counts"],
            "total_files": vr["total_files"],
            "files_with_zone": vr["files_with_zone"],
            "contaminated_count": len(vr["contaminated"]),
            "passed": vr["passed"],
        }, f, ensure_ascii=False, indent=2)

    elapsed = time.time() - job_start
    print(f"\n[완료] {region_name}/{date_str} — {final_collected}/{final_expected} ({pct:.0f}%), zone={zone_str}, {elapsed:.0f}초")
    return save_dir, region_name, date_str, pct


async def main():
    total_start = time.time()
    print("=" * 60)
    print("[배치 수집] 셔클 인사이트 데이터 배치 수집 (고속)")
    print(f"[시간] {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"[작업] {len(JOBS)}개 수집 작업")
    print("=" * 60)

    for i, job in enumerate(JOBS, 1):
        kw, period = job[0], job[1]
        tabs = job[2] if len(job) > 2 else None
        tabs_str = f" [{', '.join(tabs)}]" if tabs else " [전체]"
        print(f"  {i}. {kw} / {period}{tabs_str}")

    results = []

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=BROWSER_PROFILE, headless=False,
            viewport={"width": 1920, "height": 1080},
            slow_mo=FAST["slow_mo"],
        )
        page = context.pages[0] if context.pages else await context.new_page()

        # 전역 API 응답 수집기
        all_responses = []
        response_errors = []

        async def on_response(response):
            url = response.url
            ct = response.headers.get("content-type", "")
            is_api = "json" in ct or any(pat in url.lower() for pat in API_PATTERNS)
            if not is_api:
                return
            body = None
            try:
                body = await response.body()
            except Exception as e:
                response_errors.append({"url": url[:120], "error": str(e)})
                return
            try:
                text = body.decode("utf-8", errors="replace")
                if len(text) < 10:
                    return
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError:
                    parsed = None
                all_responses.append({
                    "tab": "수집",
                    "url": url,
                    "status": response.status,
                    "content_type": ct,
                    "body_size": len(text),
                    "body_raw": text[:5000],
                    "parsed": parsed,
                    "has_numeric_data": bool(re.search(r'\d{2,}', text[:2000])),
                })
            except Exception as e:
                response_errors.append({"url": url[:120], "error": str(e)})

        page.on("response", on_response)

        # 사이트 접속
        print("\n[접속] 사이트 접속 중...")
        try:
            await page.goto(SITE_URL, wait_until="networkidle", timeout=60000)
        except Exception:
            print("[접속] networkidle 타임아웃 — domcontentloaded로 재시도")
            await page.goto(SITE_URL, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(5000)
        await page.wait_for_timeout(3000)

        try:
            await page.wait_for_selector(
                'button[data-slot="trigger"][aria-haspopup="dialog"]',
                timeout=15000
            )
            await page.wait_for_timeout(1000)
        except Exception:
            await page.wait_for_timeout(5000)

        # 초기 모달/팝업 닫기
        for _ in range(3):
            try:
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(500)
            except Exception:
                break

        # 로그인 대기
        if "login" in page.url.lower() or "auth" in page.url.lower():
            print(f"[로그인] 브라우저에서 로그인해주세요 (최대 {LOGIN_TIMEOUT}초 대기)")
            try:
                await page.wait_for_url("**/metrics**", timeout=LOGIN_TIMEOUT * 1000)
                await page.wait_for_timeout(3000)
                print("[로그인] 로그인 완료!")
            except Exception:
                print("[로그인] 시간 초과 — 종료합니다")
                await context.close()
                return
        else:
            print("[로그인] 이미 로그인 상태")

        # 각 작업 순차 실행
        for idx, job in enumerate(JOBS, 1):
            region_kw, period = job[0], job[1]
            target_tabs = job[2] if len(job) > 2 else None
            tabs_label = f" [{', '.join(target_tabs)}]" if target_tabs else ""
            print(f"\n{'#'*60}")
            print(f"# 작업 {idx}/{len(JOBS)}: {region_kw} / {period}{tabs_label}")
            print(f"{'#'*60}")

            try:
                save_dir, region_name, date_str, collect_pct = await collect_one(
                    page, context, all_responses, response_errors,
                    region_kw, period, target_tabs=target_tabs
                )
                results.append({
                    "keyword": region_kw,
                    "period": period,
                    "region_name": region_name,
                    "date_str": date_str,
                    "save_dir": save_dir,
                    "collect_pct": collect_pct,
                    "status": "OK",
                })
            except Exception as e:
                print(f"\n[오류] 수집 실패: {e}")
                results.append({
                    "keyword": region_kw,
                    "period": period,
                    "status": f"FAIL: {str(e)[:100]}",
                })

            await page.wait_for_timeout(FAST["next_job_wait"])

        # ── 자동 재시도 루프 ──
        for retry_round in range(1, MAX_RETRY_ROUNDS + 1):
            # 실제 합산 수집률로 미달 지역 파악
            failing = []
            for r in results:
                if r["status"] != "OK":
                    continue
                true_rate = get_true_rate(r["region_name"], r["date_str"])
                r["collect_pct"] = true_rate  # 합산 수집률로 갱신
                if true_rate < REPORT_THRESHOLD:
                    failing.append(r)

            if not failing:
                print(f"\n[재시도] 모든 지역 {REPORT_THRESHOLD:.0f}% 이상 달성 — 재시도 종료")
                break

            print(f"\n{'='*60}")
            print(f"[재시도 라운드 {retry_round}/{MAX_RETRY_ROUNDS}] {len(failing)}개 지역 미달")
            for r in failing:
                print(f"  {r['region_name']}: {r['collect_pct']:.1f}%")
            print(f"{'='*60}")

            # 재시도 전 수집률 스냅샷 (개선 여부 판단용)
            prev_rates = {r["region_name"]: r["collect_pct"] for r in failing}

            for idx, r in enumerate(failing, 1):
                tabs = RETRY_TABS.get(r["region_name"])
                tabs_label = f" [{', '.join(tabs)}]" if tabs else " [전체]"
                print(f"\n{'#'*60}")
                print(f"# 재시도 {retry_round}-{idx}/{len(failing)}: {r['keyword']} / {r['period']}{tabs_label}")
                print(f"{'#'*60}")
                try:
                    save_dir, region_name, date_str, _ = await collect_one(
                        page, context, all_responses, response_errors,
                        r["keyword"], r["period"], target_tabs=tabs
                    )
                    r["collect_pct"] = get_true_rate(region_name, date_str)
                except Exception as e:
                    print(f"\n[오류] 재수집 실패: {e}")
                await page.wait_for_timeout(FAST["next_job_wait"])

            # 개선 없으면 중단 (1%p 이상 개선된 지역이 하나도 없을 때)
            any_improvement = any(
                get_true_rate(r["region_name"], r["date_str"]) > prev_rates[r["region_name"]] + 1
                for r in failing
            )
            if not any_improvement:
                print(f"\n[재시도] 라운드 {retry_round}: 개선 없음 — 재시도 중단")
                break

        # 최종 수집률 갱신
        for r in results:
            if r["status"] == "OK":
                r["collect_pct"] = get_true_rate(r["region_name"], r["date_str"])

        # 결과 요약
        total_elapsed = time.time() - total_start
        print(f"\n\n{'='*60}")
        print(f"[결과] 배치 수집 결과 요약 (총 {total_elapsed:.0f}초)")
        print(f"{'='*60}")
        for r in results:
            if r["status"] == "OK":
                pct_str = f"{r.get('collect_pct', 0):.1f}%"
                flag = "OK" if r.get("collect_pct", 0) >= REPORT_THRESHOLD else "미달"
                print(f"  [{flag}] {r['region_name']}/{r['date_str']}: {pct_str}")
            else:
                print(f"  [FAIL] {r['keyword']}/{r['period']}: {r['status']}")

        result_path = os.path.join(BASE_DATA_DIR, "_batch_result.json")
        with open(result_path, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2, default=str)

        print(f"\n[종료] 5초 후 브라우저를 닫습니다...")
        await page.wait_for_timeout(5000)
        await context.close()
        print("[종료] 완료!")

    # 수집 완료 후 리포트 생성
    generate_all_reports(results)


def generate_all_reports(results):
    """수집 완료 후 각 지역별 모니터링 리포트 HTML 생성"""
    from export_report import build_report_data, export_html, export_xlsx

    print(f"\n\n{'='*60}")
    print(f"[리포트] 모니터링 리포트 생성 시작")
    print(f"{'='*60}")

    ok_results = [r for r in results if r["status"] == "OK"]
    if not ok_results:
        print("  수집 성공 건이 없어 리포트 생성을 건너뜁니다.")
        return

    # 수집률 필터링 (세션 수집률 아닌 summary 합산 기준)
    for r in ok_results:
        r["collect_pct"] = get_true_rate(r["region_name"], r["date_str"])
    passed = [r for r in ok_results if r["collect_pct"] >= REPORT_THRESHOLD]
    skipped = [r for r in ok_results if r["collect_pct"] < REPORT_THRESHOLD]
    if skipped:
        print(f"\n  [수집률 미달 — 리포트 생성 건너뜀]")
        for r in skipped:
            print(f"    {r['region_name']}/{r.get('date_str','')}: {r['collect_pct']:.1f}% (기준 {REPORT_THRESHOLD:.0f}%)")
    ok_results = passed
    if not ok_results:
        print("  80% 이상 달성한 지역이 없어 리포트 생성을 건너뜁니다.")
        return

    # 지역별로 분석기간/비교기간 분류 (날짜 순으로 최신=분석, 이전=비교)
    region_data = {}
    for r in ok_results:
        rname = r["region_name"]
        if rname not in region_data:
            region_data[rname] = []
        region_data[rname].append(r)

    report_results = []
    for region_name, region_results in region_data.items():
        if len(region_results) < 2:
            # 기존 데이터에서 비교기간 자동 탐색
            analysis = region_results[0]
            region_dir = os.path.join(BASE_DATA_DIR, region_name)
            if os.path.isdir(region_dir):
                existing = sorted([
                    d for d in os.listdir(region_dir)
                    if os.path.isdir(os.path.join(region_dir, d))
                    and re.match(r"\d{8}_\d{8}$", d)
                    and d != os.path.basename(analysis["save_dir"])
                ])
                # 분석기간보다 이전인 것 중 가장 최근
                analysis_date = os.path.basename(analysis["save_dir"])
                candidates = [d for d in existing if d < analysis_date]
                if candidates:
                    compare_date = candidates[-1]
                    compare_path = os.path.join(region_dir, compare_date)
                    region_results.insert(0, {
                        "region_name": region_name,
                        "save_dir": compare_path,
                        "date_str": compare_date,
                        "status": "OK",
                    })
                    print(f"\n  [{region_name}] 기존 비교기간 데이터 활용: {compare_date}")
                else:
                    print(f"\n  [{region_name}] 비교기간 데이터 없음 — 리포트 건너뜀")
                    continue
            else:
                print(f"\n  [{region_name}] 기간 1개만 수집됨 — 리포트 건너뜀")
                continue

        # 날짜 순 정렬 (save_dir의 날짜 기준)
        region_results.sort(key=lambda x: x.get("date_str", ""))
        compare_dir = region_results[0]["save_dir"]  # 이전 기간
        analysis_dir = region_results[-1]["save_dir"]  # 최신 기간

        print(f"\n  [{region_name}] 리포트 생성...")
        print(f"    분석: {analysis_dir}")
        print(f"    비교: {compare_dir}")

        try:
            data = build_report_data(analysis_dir, compare_dir)
            base_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shucle_report")
            save_dir = os.path.join(base_dir, data["region"], data["period_code"])
            os.makedirs(save_dir, exist_ok=True)

            prefix = f"report_{data['region']}_{data['period_code']}"
            html_path = os.path.join(save_dir, f"{prefix}.html")
            xlsx_path = os.path.join(save_dir, f"{prefix}.xlsx")

            export_html(data, html_path)
            export_xlsx(data, xlsx_path)

            print(f"    [OK] {html_path}")
            report_results.append({"region": region_name, "html": html_path, "status": "OK"})
        except Exception as e:
            print(f"    [FAIL] {e}")
            report_results.append({"region": region_name, "status": f"FAIL: {str(e)[:100]}"})

    print(f"\n{'='*60}")
    print(f"[리포트] 생성 결과")
    print(f"{'='*60}")
    for r in report_results:
        if r["status"] == "OK":
            print(f"  [OK] {r['region']}: {r['html']}")
        else:
            print(f"  [FAIL] {r['region']}: {r['status']}")


if __name__ == "__main__":
    asyncio.run(main())
