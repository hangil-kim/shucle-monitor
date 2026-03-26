"""배치 데이터 수집 스크립트
================================
여러 지역/기간 조합을 단일 브라우저 세션에서 순차 수집합니다.

실행: python batch_collect.py
"""

import asyncio, os, json, re, sys, urllib.parse
from datetime import datetime
from playwright.async_api import async_playwright

sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from shucle_api_probe import (
    SITE_URL, BROWSER_PROFILE, BASE_DATA_DIR, TABS, API_PATTERNS,
    LOGIN_TIMEOUT, REGION_RETRY,
    select_region, select_date_range, get_save_dir,
    wait_for_chart_data, get_zone_id_for_region, verify_collected_zone,
)

# ================================================================
# 배치 수집 설정
# ================================================================
JOBS = [
    # 영덕: 비교기간 재수집 (97/126 → 100% 목표)
    ("영덕", ("2026-01-01", "2026-02-28")),
]

# 지역 키워드 → 예상 display_name 매핑 (리포트 생성 시 사용)
REGION_MAP = {
    "백운": "백운면",
    "봉양": "봉양읍",
    "검단": "검단신도시",
    "충북혁신": "충북혁신도시",
    "삼호": "삼호",
}


async def collect_one(page, context, all_responses, response_errors, region_keyword, period):
    """단일 지역/기간 데이터 수집"""
    print(f"\n{'='*60}")
    print(f"[수집] 지역={region_keyword}, 기간={period}")
    print(f"{'='*60}")

    current_tab = "초기로딩"

    # 지역 선택
    region_ok = False
    for attempt in range(1, REGION_RETRY + 1):
        print(f"\n[지역] 선택 시도 {attempt}/{REGION_RETRY}")
        if await select_region(page, region_keyword):
            region_ok = True
            break
        await page.wait_for_timeout(2000)

    if not region_ok:
        print(f"[지역] 자동 선택 실패 — 30초 수동 대기")
        print(f"[지역] 브라우저에서 직접 '{region_keyword}'를 선택해주세요!")
        for sec in range(30):
            await page.wait_for_timeout(1000)
            try:
                body_text = await page.locator("body").inner_text(timeout=1000)
                if region_keyword in body_text:
                    print(f"   [OK] {region_keyword} 감지됨")
                    region_ok = True
                    break
            except Exception:
                pass
        if not region_ok:
            print(f"[지역] 선택 확인 불가 — 현재 상태로 계속 진행")

    # 기간 설정
    await select_date_range(page, period)

    # 이전 응답 제거 (지역/기간 변경 전 데이터 오염 방지)
    old_count = len(all_responses)
    all_responses.clear()
    response_errors.clear()
    if old_count:
        print(f"\n[정리] 이전 응답 {old_count}개 제거")

    # 차트 재로딩 대기
    await page.wait_for_timeout(5000)

    # 초기 차트 데이터 로딩 대기
    print("\n[초기] 초기 차트 데이터 로딩 대기 중...")
    init_total, init_charts = await wait_for_chart_data(
        all_responses, 0, "초기로딩", timeout=60, stable_secs=10
    )
    print(f"   [OK] 초기로딩: 전체 {init_total}개 (차트 {init_charts}개)")

    # 저장 경로 결정
    save_dir, region_name, date_str = await get_save_dir(page)

    os.makedirs(save_dir, exist_ok=True)
    print(f"\n[저장] 경로: {os.path.abspath(save_dir)}")
    print(f"   지역: {region_name} / 기간: {date_str}")

    # 스크린샷
    confirm_path = os.path.join(save_dir, "00_region_confirm.png")
    await page.screenshot(path=confirm_path)

    # 각 탭 순회
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
            await page.wait_for_timeout(10000)

            # 스크롤로 lazy-load 트리거
            for _ in range(5):
                await page.mouse.wheel(0, 800)
                await page.wait_for_timeout(1500)
            await page.mouse.wheel(0, -5000)
            await page.wait_for_timeout(2000)
            for _ in range(5):
                await page.mouse.wheel(0, 800)
                await page.wait_for_timeout(1500)

            tab_total, tab_charts = await wait_for_chart_data(
                all_responses, tab_start_idx, tab_name, timeout=60, stable_secs=8
            )

            if tab_charts == 0:
                print(f"   [재시도] 차트 데이터 0개 — 추가 대기...")
                await page.mouse.wheel(0, -5000)
                await page.wait_for_timeout(3000)
                for _ in range(7):
                    await page.mouse.wheel(0, 800)
                    await page.wait_for_timeout(2000)
                tab_total, tab_charts = await wait_for_chart_data(
                    all_responses, tab_start_idx, tab_name, timeout=30, stable_secs=8
                )

            print(f"   -> 전체 {tab_total}개 (차트 데이터 {tab_charts}개)")

        except Exception as e:
            print(f"   [FAIL] 탭 전환 실패: {e}")

    # slice_id → slice_name 매핑 구축
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

    # 수집된 차트 slice_id 세트
    collected_sids = set()
    for r in all_responses:
        url = r["url"]
        if "chart/data" not in url and "explore_json" not in url:
            continue
        m = re.search(r"slice_id(?:%22%3A|[\":\s]+)(\d+)", url)
        if m:
            collected_sids.add(int(m.group(1)))

    SKIP_SIDS = {3756, 3757}
    expected_sids = set(slice_map.keys())
    missing_sids = expected_sids - collected_sids - SKIP_SIDS

    print(f"\n[검증] 차트 수집: 기대 {len(expected_sids)}개, 수집 {len(collected_sids)}개, 미수집 {len(missing_sids)}개")

    # 누락 차트 재수집
    if missing_sids:
        print(f"[재수집] 누락 {len(missing_sids)}개 차트 직접 API 호출...")
        superset_frame = None
        for frame in page.frames:
            if "superset" in frame.url:
                superset_frame = frame
                break

        if superset_frame:
            retry_ok = 0
            for sid in sorted(missing_sids):
                did = slice_dashboard.get(sid)
                sname = slice_map.get(sid, f"slice_{sid}")
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
                await page.wait_for_timeout(500)
            print(f"   재수집 성공: {retry_ok}개")

    final_collected = len(collected_sids)
    final_expected = len(expected_sids - SKIP_SIDS)
    pct = final_collected / final_expected * 100 if final_expected else 0
    print(f"   최종 수집률: {final_collected}/{final_expected} ({pct:.1f}%)")

    # 결과 저장 전 기존 데이터 정리 (수집 완료 후에만 삭제하여 중단 시 유실 방지)
    old_files = [f for f in os.listdir(save_dir)
                 if f.endswith(".json") and not f.startswith("00")]
    if old_files:
        for f in old_files:
            os.remove(os.path.join(save_dir, f))
        print(f"\n[정리] 기존 데이터 파일 {len(old_files)}개 삭제 (새 데이터로 교체)")

    tab_summary = {}
    for r in all_responses:
        tab_summary.setdefault(r["tab"], []).append(r)

    for tab, responses in tab_summary.items():
        for j, r in enumerate(responses):
            if r["has_numeric_data"] and r["body_size"] > 100:
                fname = f"{tab.replace(' ', '_')}_{j:02d}.json"
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

    with open(os.path.join(save_dir, "_summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    # zone_id 검증
    expected_zone_id = await get_zone_id_for_region(context, region_name)
    vr = verify_collected_zone(save_dir, expected_zone_id)
    print(f"   zone 검증: {'PASS' if vr['passed'] else 'FAIL' if vr['passed'] is False else 'SKIP'}")

    verify_path = os.path.join(save_dir, "_zone_verify.json")
    with open(verify_path, "w", encoding="utf-8") as f:
        json.dump({
            "region_name": region_name,
            "region_keyword": region_keyword,
            "expected_zone_id": expected_zone_id,
            "primary_zone_id": vr["primary_zone"],
            "zone_counts": vr["zone_counts"],
            "total_files": vr["total_files"],
            "files_with_zone": vr["files_with_zone"],
            "contaminated_count": len(vr["contaminated"]),
            "passed": vr["passed"],
        }, f, ensure_ascii=False, indent=2)

    print(f"\n[완료] {region_name}/{date_str} 수집 완료 ({final_collected}/{final_expected})")
    return save_dir, region_name, date_str


async def main():
    print("=" * 60)
    print("[배치 수집] 셔클 인사이트 데이터 배치 수집")
    print(f"[시간] {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"[작업] {len(JOBS)}개 수집 작업")
    print("=" * 60)

    for i, (kw, period) in enumerate(JOBS, 1):
        print(f"  {i}. {kw} / {period}")

    results = []

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=BROWSER_PROFILE, headless=False,
            viewport={"width": 1920, "height": 1080}, slow_mo=300,
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
            await page.goto(SITE_URL, wait_until="networkidle", timeout=120000)
        except Exception:
            print("[접속] networkidle 타임아웃 — domcontentloaded로 재시도")
            await page.goto(SITE_URL, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(10000)
        await page.wait_for_timeout(5000)

        try:
            await page.wait_for_selector(
                'button[data-slot="trigger"][aria-haspopup="dialog"]',
                timeout=30000
            )
            await page.wait_for_timeout(2000)
        except Exception:
            await page.wait_for_timeout(10000)

        # 로그인 대기
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

        # 각 작업 순차 실행
        for idx, (region_kw, period) in enumerate(JOBS, 1):
            print(f"\n\n{'#'*60}")
            print(f"# 작업 {idx}/{len(JOBS)}: {region_kw} / {period}")
            print(f"{'#'*60}")

            try:
                save_dir, region_name, date_str = await collect_one(
                    page, context, all_responses, response_errors,
                    region_kw, period
                )
                results.append({
                    "keyword": region_kw,
                    "period": period,
                    "region_name": region_name,
                    "date_str": date_str,
                    "save_dir": save_dir,
                    "status": "OK",
                })
            except Exception as e:
                print(f"\n[오류] 수집 실패: {e}")
                results.append({
                    "keyword": region_kw,
                    "period": period,
                    "status": f"FAIL: {str(e)[:100]}",
                })

            # 다음 작업 전 짧은 대기
            await page.wait_for_timeout(3000)

        # 결과 요약
        print(f"\n\n{'='*60}")
        print(f"[결과] 배치 수집 결과 요약")
        print(f"{'='*60}")
        for r in results:
            status = r["status"]
            kw = r["keyword"]
            period = r["period"]
            if status == "OK":
                print(f"  [OK] {r['region_name']}/{r['date_str']} (keyword={kw})")
            else:
                print(f"  [FAIL] {kw}/{period}: {status}")

        # 결과 JSON 저장
        result_path = os.path.join(BASE_DATA_DIR, "_batch_result.json")
        with open(result_path, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2, default=str)
        print(f"\n[저장] 결과: {result_path}")

        print(f"\n[종료] 10초 후 브라우저를 닫습니다...")
        await page.wait_for_timeout(10000)
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

    # 성공한 수집 결과를 지역별로 그룹핑
    ok_results = [r for r in results if r["status"] == "OK"]
    if not ok_results:
        print("  수집 성공 건이 없어 리포트 생성을 건너뜁니다.")
        return

    # 지역별로 분석기간/비교기간 분류
    region_data = {}  # region_name -> {"analysis": save_dir, "compare": save_dir}
    for r in ok_results:
        rname = r["region_name"]
        period = r["period"]
        if rname not in region_data:
            region_data[rname] = {}
        # 분석기간: 2026-01-01~2026-03-25, 비교기간: 2025-10-01~2025-12-31
        if isinstance(period, (tuple, list)):
            start = period[0]
        else:
            start = str(period)
        if start.startswith("2026"):
            region_data[rname]["analysis"] = r["save_dir"]
        else:
            region_data[rname]["compare"] = r["save_dir"]

    report_results = []
    for region_name, dirs in region_data.items():
        analysis_dir = dirs.get("analysis")
        compare_dir = dirs.get("compare")

        if not analysis_dir:
            print(f"\n  [{region_name}] 분석기간 데이터 없음 — 건너뜀")
            continue

        print(f"\n  [{region_name}] 리포트 생성 중...")
        print(f"    분석기간: {analysis_dir}")
        if compare_dir:
            print(f"    비교기간: {compare_dir}")

        try:
            data = build_report_data(analysis_dir, compare_dir)

            # 저장 경로
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

    # 리포트 결과 요약
    print(f"\n{'='*60}")
    print(f"[리포트] 생성 결과 요약")
    print(f"{'='*60}")
    for r in report_results:
        if r["status"] == "OK":
            print(f"  [OK] {r['region']}: {r['html']}")
        else:
            print(f"  [FAIL] {r['region']}: {r['status']}")


if __name__ == "__main__":
    asyncio.run(main())
