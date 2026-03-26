# Shucle Monitor

## Project Overview
셔클 인사이트(insight.shucle.com/metrics) 데이터 자동 수집 및 모니터링 프로젝트

## Tech Stack
- Python 3.14
- Playwright (브라우저 자동화)
- Apache Superset 기반 대시보드 (superset1.shucle.com)
- Mapbox (지도 데이터 - 정류장 이용 탭)

## Architecture
- 인증: Superset guest_token 방식 (embedded dashboard)
- API: `superset1.shucle.com/api/v1/chart/data?form_data={"slice_id":XXXX}&dashboard_id=YY`
- 대시보드 ID 매핑:
  - 88: 호출 탑승
  - 112: 서비스 품질
  - 149: 가호출 수요
  - 115: 지역 회원
  - 113: 정류장 이용
  - 114: 차량 운행
- Zone API: `api-coco.shucle.com/v1/zone/list?target=real`
- 브라우저 프로필: `./shucle_browser_profile` (로그인 세션 유지)

## UI 구조 (지역 선택)
- 상단 헤더에 드롭다운 버튼 3개: DRT 유형 / 지역 선택 / 전체 유형
- 지역 드롭다운: `button[data-slot="trigger"][aria-haspopup="dialog"]` (DRT/유형 제외한 것)
- 클릭 시 팝오버에 검색창 + 지역 목록 표시
- 검색창: `input[placeholder*="검색"]`
- 지역 옵션: `div[class*="zone-select__Value"]`
- 검단 zone code: GEOMDAN, display_name: 검단신도시, id: 100

## UI 구조 (기간 선택)
- `date-range-picker__Shortcut` 클래스 div: 1주 / 4주 / 12주 단축 버튼
- `date-range-picker__Root` inner_text: 개행으로 구분됨 ("2026\n.\n2\n.\n18\n.\n-\n...")→ 개행 제거 후 파싱
- 설정값: `DATE_RANGE = "1주"` (기본), 커스텀: `DATE_RANGE = ("2026-02-01", "2026-02-20")`
- 날짜 세그먼트: `div[data-slot="segment"][role="spinbutton"]` — `contenteditable="true"`
  - `data-type`: year / month / day / literal
  - `aria-label`: "년, 시작일, " / "월, 종료일, " 등으로 시작일/종료일 구분
  - 년도: 숫자 키 입력 (Digit0~9), 월/일: ArrowUp/Down으로 조정

## 저장 경로 구조
- `shucle_data/{지역명}/{시작일_종료일}/` (예: `shucle_data/검단신도시/20260218_20260224/`)
- 지역명: UI 드롭다운에서 자동 읽기
- 날짜: date-range-picker에서 자동 파싱 (YYYYMMDD_YYYYMMDD)

## Project Structure
- `shucle_api_probe.py` — API 탐색 스크립트 (Step 1)
- `analyze_data.py` — 수집 데이터 분석/테이블 출력 스크립트
- `analyze_values.py` — 수집 데이터 지표+값 요약 테이블 출력 (slice_name 매핑 포함)
- `debug_region.py` — 지역 선택 UI 디버그 스크립트
- `debug_datepicker.py` — 기간 선택 UI 디버그 스크립트
- `monitoring_report.py` — KPI 분석 체계 기반 모니터링 보고서 생성 (수요→품질→공급→성장)
- `export_report.py` — 보고서 파일 내보내기 (HTML/DOCX/XLSX 3종)
- `batch_collect.py` — 다지역/다기간 배치 데이터 수집 + 리포트 자동 생성 스크립트
- `shucle_auto_monitoring_overview.md` — 자동 데이터 수집 및 모니터링 리포트 기획 배경 문서
- `shucle_auto_monitoring_overview.pdf` — 기획 배경 문서 PDF 산출물
- `shucle_screenshot.py` — 인사이트 탭별 전체 페이지 스크린샷 캡처 스크립트
- `shucle_data/{지역}/{날짜}/` — 수집 결과 저장 (탭별 JSON + _summary.json + _zone_verify.json)
- `shucle_report/{지역}/{기간}/` — 내보낸 보고서 파일 저장
- `shucle_screenshots/{타임스탬프}/{대분류}/{소분류}.png` — 탭별 전체 페이지 스크린샷

## Development Guidelines
- 모든 코딩 작업 후 이 파일에 작업 내용을 기록할 것
- 새 채팅 세션에서도 작업 연속성을 유지하기 위한 컨텍스트 파일로 활용
- Windows 환경: stdout UTF-8 강제 설정 필요 (cp949 이모지 오류 방지)
- 로그인은 수동, 나머지 자동화

## Work Log
<!-- 최신 작업이 위에 오도록 역순 기록 -->

### 2026-03-26
- 5개 지역 분기별 모니터링 리포트 생성 (분석: 2026.01.01~03.25, 비교: 2025.10.01~12.30)
  - 대상 지역: 백운면, 봉양읍, 검단신도시, 충북혁신도시, 삼호
  - 데이터 수집: 5지역 × 2기간 = 10건 전부 126/126 (100%) 달성
  - 비교기간 가호출성공률/DAU 2개 KPI 누락 → 2025년 시점 대시보드에 해당 차트 미존재 (수집 문제 아님)
  - 리포트 저장: `shucle_report/{지역}/20260101_20260325/report_{지역}_20260101_20260325.{html,xlsx}`
- `batch_collect.py` 신규 생성: 다지역/다기간 배치 데이터 수집 스크립트
  - 단일 브라우저 세션에서 여러 지역/기간 조합을 순차 수집
  - 수집 완료 후 자동 HTML/XLSX 리포트 생성 (`generate_all_reports()`)
  - 기존 데이터 삭제를 수집 완료 후로 이동하여 중단 시 데이터 유실 방지
  - 지역 키워드 → display_name 매핑 (`REGION_MAP`)
- `shucle_api_probe.py` 과거 연도 날짜 설정 기능 추가
  - **근본 문제**: 시작일 year 세그먼트가 ArrowDown/digit 입력 모두 반응하지 않음 (2025년 설정 불가)
  - **해결**: `Date.now()` JavaScript 오버라이드로 프리셋 "12주"가 과거 연도 시작일을 계산하도록 우회
    - fake_today = target_start + 91일 → "12주" 클릭 시 시작 연도 2025 확보
    - 프리셋 처리 후 Date.now() 즉시 복원
  - 종료일 31일 설정 불가 문제: fake Date.now()로 인한 max day=30 제약
    - Date.now() 복원 후에도 캐시된 max day 미갱신 → 종료일 12.30까지만 가능
    - 12.31 설정 시 유효성 오류("시작일은 종료일 이전이어야 합니다") 발생 → 데이터 로딩 차단
    - **최종 결정**: 12.30으로 수집 (92일 중 1일 = 1.1% 차이, 분석 영향 무시)
  - `set_date_segment()` year 실패 시 4가지 대안 순차 시도 추가 (triple-click+입력, fill, JS, Ctrl+A)
    - 결과: 4가지 모두 실패 → 시작 연도 세그먼트는 UI로 변경 불가 확정
  - 최종 검증+보정 루프 추가: UI 표시 텍스트 기반 날짜 검증 (aria-valuenow와 표시 불일치 대응)
- `batch_collect.py` 사이트 접속 타임아웃 개선
  - networkidle 120초 → 실패 시 domcontentloaded 60초 폴백

### 2026-03-16
- `shucle_screenshot.py` 신규 생성: 인사이트 탭별 전체 페이지 스크린샷 자동 캡처
  - 좌측 사이드바 4개 대분류 탭 순회: 운행/매출/통계/지역
  - 각 대분류 내 소분류 탭 자동 탐지 및 순회 (운행 6개, 매출 1개, 통계 4개, 지역 1개 = 총 12개)
  - Superset iframe 내부 `document.documentElement` 스크롤로 전체 페이지 캡처
  - 스크롤 캡처 → Pillow로 중복 영역 제거 후 1장 합성
  - iframe lazy-load 트리거: 2차 왕복 스크롤로 차트 데이터 완전 로딩
  - 좌측 사이드바: `button[class*="navigation__Menu"]` 셀렉터로 대분류 탭 클릭
  - 저장 구조: `shucle_screenshots/{타임스탬프}/{대분류}/{순번_소분류}.png`
  - 검단신도시 기준 캡처 완료: 12개 스크린샷, 최대 10127px(차량 운행)
- `shucle_auto_monitoring_overview.md` 이미지 height=200 가운데 정렬 + PDF 마진 14mm 적용

### 2026-03-12
- `shucle_auto_monitoring_overview.md` 기획 배경 문서 작성 및 PDF 산출
  - 목차: 문제의식 / 작동 프로세스 / 데이터 분석 체계 / 활용 방식 / 한계 / 모니터링 리포트 예시
  - 두괄식 체언 종결 문체로 정리, A4 2페이지 분량 맞춤
  - PDF 변환: markdown → HTML → Playwright PDF 출력 방식 (WeasyPrint는 GTK 미설치로 불가)
  - 산출물: `shucle_auto_monitoring_overview.pdf`

### 2026-03-09
- 인천 검단신도시 주간 모니터링 리포트 생성 (분석: 03.02~03.08, 비교: 02.23~03.01)
  - 데이터 수집: 비교기간 20260223_20260301 (126+ slices, zone_id=100, PASS), 분석기간 20260302_20260308 (126+ slices, zone_id=100, PASS)
  - 분석기간 수집 시 지역명 "unknown" 저장 버그 발생 → 수동 폴더 이동으로 해결
  - 주요 변동: 운행차량 대수 4.86→4.29대 (-11.8%, 유일한 ±10% 초과 항목)
  - 수요 안정: 호출 -1.3%, 이동완료 -2.2%, 탑승객 -7.0%
  - 성장 긍정: 신규회원 50→73명 (+46.0%), 누적 8,850명
  - 리포트 저장: `shucle_report/검단신도시/20260302_20260308/report_*.{html,xlsx}`

### 2026-03-03
- `select_region()` 버그 수정: Values 직접 매칭 우선순위 변경
  - 문제: Options(전체주소)에서 키워드 매칭 후 같은 인덱스 Value 클릭 → 제천의 경우 "충청북도제천시백운면봉양읍" 1개 Option에 백운면/봉양읍 2개 Value 대응 → "봉양" 키워드가 백운면을 선택하는 버그
  - 수정: JS 매칭 순서 변경 — 1순위: Values 텍스트에 키워드 직접 포함 (keyword-in-value), 2순위: Options 인덱스 매칭
  - 결과: "봉양" → "봉양읍" 정상 선택 (keyword-in-value 방식)
- 제천 봉양읍/백운면 월간 모니터링 리포트 생성 (분석: 2026-02, 비교: 2026-01)
  - 데이터 수집: 봉양읍 20260201_20260228 (126/126, zone_id=171, PASS), 백운면 기존 데이터 재활용 (126/126, zone_id=142, PASS)
  - 비교기간: 기존 20260101_20260131 데이터 재활용 (각 126/126, PASS)
  - 백운면 주요 변동: 총 탑승객 -10.0%, 평균 경로이탈비중 -13.9%(개선), DAU +5.9%
  - 봉양읍 주요 변동: 평균 우회비율 +10.3%(악화), 신규회원 -19.6%
  - 리포트 저장: `shucle_report/{봉양읍,백운면}/20260201_20260228/report_*.{html,xlsx}`

### 2026-02-27
- 리포트 목차 구조 변경 (4섹션 체계)
  - 1. 1차 핵심지표 요약 (기존 유지)
  - 2. 2차 세부지표_변동 (1차 핵심지표 ±10% 이상 변동) — 기존 2번에서 제목 변경
  - 3. 2차 세부지표_안정 (10% 이내 변동) — 신규 추가, 트리거 안 된 KPI의 드릴다운도 동일 양식으로 표출
  - 4. 핵심 해석 (기존 3번 → 4번으로 이동)
  - monitoring_report.py: stable_list 수집 + 섹션 3 출력 로직 추가
  - export_report.py: stable_sections 데이터 생성 + HTML/DOCX/XLSX 렌더링 추가, _build_dd_rows() 헬퍼 함수로 리팩터링
- 2차 세부지표 "시간대별 이동시간" → "피크/비피크 시간대 이동시간" 분리
  - 카테고리형 12시간대 데이터 단일 값 표현 불가 문제 해결 (가호출 성공률과 동일 패턴)
  - `_build_hour_volume()`: 볼륨 집계 공통화, `_find_best_consecutive_2h()`: 피크/비피크 공통 탐지
  - `extract_peak_travel()` / `extract_offpeak_travel()` / `_extract_travel_at()` 함수 추가
  - `_resolve_dynamic_name()`: 피크/비피크/성공률 동적 이름 생성 공통 함수로 리팩터링
- 2차 세부지표 "대기시간 구간 분포" → "장시간 대기(30분+) 비율"로 변경
  - 9개 구간별 건수 합산(무의미) → 30분 이상 구간 비율(%)로 변환
  - `_extract_long_wait_ratio()`: 컬럼명에서 하한값 파싱, threshold 이상 비율 계산
  - `get_drilldown_value()`에 `long_wait_ratio` 모드 추가
- 평균 대당 운행거리 KPI 버그 수정: "월간 평균 대당 운행거리"(월 누적) 차트가 먼저 매칭되어 3,108km 표시 → match에 excludes=["월간", "주간"] 추가하여 일간 차트(216km)만 매칭
- 리포트 용어 일반화: '전주'→'비교기간', '이번주'→'분석기간' 일괄 변경 (주간/월간 비교 모두 대응)
  - monitoring_report.py 31건, export_report.py 19건 변경
- 2차 세부지표 섹션 제목 변경: "비교기간 대비 ±10% 이상 변동 항목" → "1차 핵심지표가 비교기간 대비 ±10% 이상 변동 항목만 표시"
- 2차 세부지표 "시간대별 가호출 성공률" → "피크 시간대(N~M시) 가호출 성공률"로 변경
  - 카테고리형 12시간대 데이터를 단일 값으로 표현 불가 문제 해결
  - `_find_peak_hours()`: 볼륨 차트("시간대별 실시간 호출 결과")에서 연속 2시간 피크 자동 탐지
  - `extract_peak_success()`: 피크 시간대 가호출 성공률 추출
  - `_extract_success_at()`: 주어진 시간대의 성공률 추출 (비교기간용 - 분석기간 피크 시간 재사용)
  - `get_drilldown_value()`에 `peak_success` 모드 추가, `_peak_hours` 임시 저장으로 비교기간 동일 피크 적용
  - 동적 이름 처리: generate_report()와 build_report_data()에서 "피크 시간대(9~11시) 가호출 성공률" 형태로 표시
- 제천 봉양읍/백운면 월간 모니터링 리포트 생성 (기준: 2026-01, 비교: 2025-12)
  - 데이터 수집 4건 완료 (모두 126/126, zone_id PASS):
    - 봉양읍 20260101_20260131 (zone_id=171)
    - 봉양읍 20251201_20251231 (zone_id=171)
    - 백운면 20260101_20260131 (zone_id=142)
    - 백운면 20251201_20251231 (zone_id=142)
  - 봉양읍 주요 변동: 대당 운행거리 +1339%, 경로이탈비중 +19.5%, 신규회원 -33.3%
  - 백운면 주요 변동: 가호출 성공률 -10.8%, 대당 운행거리 +58.5%, 경로이탈비중 +82.3%, 신규회원 -32.7%
  - 리포트 저장: `shucle_report/{봉양읍,백운면}/20260101_20260131/report_*.{html,xlsx}`
- `shucle_api_probe.py` date picker 날짜 설정 로직 대폭 수정 (과거 기간 설정 시 오류 해결)
  - **근본 문제**: 시작일 year 세그먼트가 digit typing, ArrowDown 모두에 반응하지 않음
  - **종료일 제약**: 현재 연도(2026)에서 종료일 월을 미래(>현재월)로 설정 불가
  - **해결 방법 (5차 시도 끝 성공)**:
    1. 프리셋("12주"/"1주")으로 시작 연도 맞추기 (시작일 연도 직접 변경 불가 우회)
    2. 시작일 월→1, 일→1 임시 최소화 (종료일 연도를 과거로 내릴 공간 확보)
    3. 종료일 연도→월→일 순서 설정 (과거 연도 먼저 → 미래 월 제약 회피)
    4. 시작일 월→일 순서 복원 (종료일 확정 후 안전하게)
  - `set_date_segment()` 개선: year용 ArrowUp/Down 지원 (diff ≤10), 디버그 로그 추가
- `monitoring_report.py` 2차 세부지표 "활성 회원 연령대" → "활성 회원 평균연령"으로 변경
  - `extract_age_avg()` 함수 신규 추가: 연령대 카테고리를 대표 연령(N+5)으로 변환 후 가중평균 계산
  - `get_drilldown_value()`에 `age_avg` 파라미터 지원 추가
  - 봉양 결과: 전주 66.1세 → 이번주 63.7세 (-2.4세)
- `monitoring_report.py` 2차 세부지표 "탑승객 종류별" + "연령대별 호출 결과" → 연령대별 호출 비율 3개로 통합 분리
  - 삭제: "탑승객 종류별" (전체 합산 = 총 탑승객 수와 동일, 무의미), "연령대별 호출 결과" (10개 연령대 × 4개 결과 단순 평균, 무의미)
  - 추가: "고령자(60+) 호출 비율", "성인(20~50대) 호출 비율", "어린이/청소년 호출 비율" (연령대별 실시간 호출 차트 기반)
  - `extract_category_ratio()` 확장: `category_value`에 리스트 전달 시 복수 카테고리 합산 비율 계산
  - 봉양 결과: 고령자 64.4%→73.4%(+9.0%p), 성인 24.4%→20.6%(-3.8%p), 어린이/청소년 11.2%→5.9%(-5.2%p)
- 봉양읍 XLSX 재저장 완료 (이전 세션 PermissionError로 실패했던 건)
  - 기존 파일이 잠겨 있어 `report_봉양읍_20260219_20260225_v2.xlsx`로 저장

### 2026-02-26
- `monitoring_report.py` 2차 세부지표 "호출 방식별 비율" → 개별 비율 3개로 분리
  - 삭제: "호출 방식별 비율" (카테고리별 건수 단순 평균으로 무의미한 값)
  - 추가: "전화 호출 비율", "앱 호출 비율", "현장 호출 비율" (각각 전체 대비 %)
  - 현장 호출 = caller_type1이 NULL인 항목
  - `extract_category_ratio()` 함수 신규 추가: 카테고리형 차트에서 특정 카테고리의 비율(0~1) 추출
  - `get_drilldown_value()`에 `cat_ratio` 파라미터 지원 (`"cat_ratio" in dd_def`로 키 존재 여부 판별)
  - 2차 드릴다운 값 포맷에 `is_pct` 지원 추가 (% 표시, 변동값은 %p 단위)
  - FRAMEWORK 드릴다운 정의: 감소/증가 트리거 양쪽에 3개 비율 지표 추가
- `monitoring_report.py` 리포트 데이터 누락 문제 3건 수정
  - 원인 1: `find_chart`가 카테고리형 차트를 시계열보다 먼저 매칭 → 실시간 호출 건수 "-"
    - 수정: `find_chart`에 `prefer_timeseries=True` 옵션 추가, coltypes에 2(timestamp) 있는 차트 우선 매칭
  - 원인 2: 2차 드릴다운에서 카테고리형 차트(연령대별/시간대별) 파싱 미지원 → "-"
    - 수정: `extract_categorical_sum()` 함수 추가, `get_drilldown_value`에서 시계열 실패 시 카테고리형 폴백
  - 원인 3: 봉양읍 전주 데이터 119/126 수집 부족 (대당 탑승객 수, 경로이탈비중 등 누락)
    - 수정: 전주(20260212_20260218) 재수집 → 126/126 (100%) 달성
  - 결과: 1차 핵심지표 15개 전부 "-" 없이 값 표시, 2차 드릴다운 카테고리형 차트도 정상 출력
- `shucle_api_probe.py` 차트 수집 완전성 검증 및 누락 재수집 기능 추가
  - 문제: 수집 시마다 지표 수가 달라짐 (108~126개로 편차, 기대값 128개 중 지도 2개 제외 = 126개)
  - 스크롤 강화: 기존 3×500px → 5×800px + 상단 복귀 후 재스크롤 (2차 lazy-load 트리거)
  - 수집 후 검증: dashboard/charts API에서 기대 차트 128개 목록 추출, 수집된 slice_id와 비교
  - 누락 차트 재수집: iframe 내 fetch()로 chart/data API 직접 호출하여 누락분 보충
  - SKIP_SIDS = {3756, 3757}: 정류장 이용 지도 히트맵 (chart/data API 미사용, 수집 불가)
  - 정규식 수정: `slice_id[\":%3A]+(\d+)` → `slice_id(?:%22%3A|[\":\s]+)(\d+)` (URL 인코딩 패턴 처리)
  - 결과: 봉양/백운 4개 데이터셋 모두 126/126 (100.0%) 달성
- `monitoring_report.py` 리포트 실행 시 HTML/XLSX 파일 자동 저장 기능 추가
  - `auto_export()` 함수: `export_report.py`의 `build_report_data`, `export_html`, `export_xlsx` 호출
  - `main()` 끝에서 `generate_report()` 후 `auto_export()` 자동 실행
  - 저장 경로: `shucle_report/{지역}/{기간}/report_{지역}_{기간}.{html,xlsx}`
- 제천 봉양읍/백운면 데이터 수집 및 주간 비교 리포트 생성
  - 봉양읍: 이번주(20260219_20260225) + 전주(20260212_20260218), zone_id=171, PASS
  - 백운면: 이번주(20260219_20260225) + 전주(20260212_20260218), zone_id=142, PASS
  - 봉양 주요 변동: 이동완료+42%, 대기시간+36%, 가호출성공률-23%, DAU+38%
  - 백운 주요 변동: 이동완료+67%, 가호출성공률-12%, DAU+58%, 신규회원+80%
- KPI 프레임워크 확장: 1차 핵심지표 3개 + 2차 세부지표 드릴다운 1개 추가
  - 수요: "대당 탑승객 수" (차량 대당 탑승객 평균, 단위: 명)
  - 공급: "평균 대당 운행거리" (평균 대당 운행거리, 단위: km)
  - 공급: "평균 경로이탈비중" (드라이버 내비게이션 경로 준수 차트, flat_col 방식)
  - 실시간 호출 건수 드릴다운에 "호출 방식별 비율" 추가 (카테고리형 데이터)
  - `extract_flat_avg()` 함수 신규 추가: 타임스탬프 없는 차트의 특정 컬럼 평균값 추출
  - `get_kpi_value()`에 `flat_col` 파라미터 지원 추가
- `export_report.py` 신규 생성: 모니터링 보고서를 HTML/DOCX/XLSX 3종 파일로 내보내기
  - `monitoring_report.py`의 데이터 함수 재사용 (load_charts, get_kpi_value 등)
  - `build_report_data()`: 보고서 데이터 구조화 수집 (1차/2차/핵심해석)
  - HTML: 인라인 CSS, 상태열 빨강/파랑 색상, 표 테두리
  - DOCX: python-docx, Word 테이블 + 폰트 색상 (RGBColor)
  - XLSX: openpyxl, 시트 분리 (1차/2차/핵심해석), 셀 색상
  - 저장 경로: `shucle_report/{지역}/{기간}/report_{지역}_{기간}.{html,docx,xlsx}`
  - 사용법: `python export_report.py <데이터_디렉토리> [비교_디렉토리]`
- 1차 핵심지표 상태열 ANSI 색상 적용: 부정적(악화/감소)=빨강, 긍정적(개선/증가)=파랑
  - `color_status()`, `strip_ansi()` 함수 추가, `kr_len()` ANSI 코드 무시 처리
  - Windows ANSI 지원 활성화 (`SetConsoleMode`)
- 2차 세부지표 '확인 포인트' → '포인트'로 변경, 예상 원인 중심 서술로 전환
  - 의문형("~인지 확인") 제거 → 원인 단정형("~에 따른 ~") 으로 FRAMEWORK 전체 reason 재작성
- 보고서 출력 양식 고정: 3단 구조 (1차 핵심지표 표 → 2차 세부지표 표 → 핵심 해석)
  - 1차: 구분/지표/전주/이번주/변동값/변동률/상태 7열 테이블, 카테고리별 구분선
  - 2차: 구분/세부지표/전주/이번주/변동값/변동률/포인트 7열 (±10% 초과 항목만)
  - 핵심 해석: 데이터 기반 자유 해석 (운행일수, 특정일 극단치, 지표간 교차 분석 등)
- 영덕관광 전주(20260212~20260218) 데이터 수집 완료 → 주간 비교 분석 실행
  - 216개 파일 수집, 122개 차트 데이터, zone_id=163 100% (오염 0건)
  - 비교 사용법: `python monitoring_report.py shucle_data/영덕관광/20260219_20260225 shucle_data/영덕관광/20260212_20260218`
  - 주요 변동: 호출-27%, 이동완료-41%, 대기시간+156%, 가호출성공률-83%, DAU+67%, 신규회원+180%
- `monitoring_report.py` 모니터링 보고서 생성 스크립트 신규 작성
  - 분석 체계: 수요→품질→공급→성장 4개 카테고리, 11개 1차 핵심 지표
  - 1차 지표 변동 시 2차 원인 분석 지표 드릴다운 (전주 대비 ±10% 기준)
  - 단일 기간: 값/일별추이/관련지표 표시 + 일별 편차 경고
  - 비교 기간: 전주 대비 변동률 + 트리거 조건 충족 시 드릴다운 상세 분석
  - 사용법: `python monitoring_report.py <데이터_디렉토리> [비교_디렉토리]`
- 실행 시 지역/기간 강제 입력 방식으로 변경
  - 기존: `REGION_KEYWORD`, `DATE_RANGE` 코드 내 하드코딩
  - 변경: `prompt_settings()` 함수로 실행 시 input() 입력 (지역/기간 모두 필수)
  - 프리셋(1주/4주/12주) + 커스텀(YYYY-MM-DD,YYYY-MM-DD) 형식 지원
- 수집 후 zone_id 자동 검증 기능 추가
  - `get_zone_id_for_region(context, region_name)`: zone API 조회 → 지역명에 해당하는 zone_id 반환 (Playwright context request + urllib 폴백)
  - `verify_collected_zone(save_dir, expected_zone_id)`: 수집 파일 전체 스캔, `zone_id IN ('NNN')` 패턴 추출 후 예상 zone_id와 대조
  - 검증 결과: PASS/FAIL/SKIP + 오염 파일 목록 출력 + `_zone_verify.json` 저장
- 데이터 오염 문제 조사 완료 및 수정
  - 원인 1: `select_region(page)` 호출 시 REGION_KEYWORD 미전달 → 항상 기본값 "검단" 사용
  - 수정: `select_region(page, REGION_KEYWORD)` 명시적 전달, 함수 default=None으로 변경
  - 원인 2: `all_responses`가 페이지 로드부터 캡처 → 이전 지역 데이터 혼입
  - 수정: 지역/기간 설정 후 `all_responses.clear()` + `response_errors.clear()` 추가
  - 원인 3: 동일 디렉토리 재실행 시 이전 JSON 파일 잔존
  - 수정: 저장 전 기존 .json 파일 삭제 (00_* 제외) 로직 추가
  - 원인 4: 페이지 로드 후 UI 요소 미준비 상태에서 지역 선택 시도
  - 수정: `wait_for_selector('button[aria-haspopup="dialog"]', timeout=15000)` 추가
- 영덕관광 1주(20260219~20260225) 데이터 수집 완료
  - 전체 197개 파일, zone_id 오염 0개 확인 (163=영덕 145개, Jinja 6개, zone필터없음 46개)
  - 6개 탭 108개 차트 매핑 100% 성공 (미매핑 0개)

### 2026-02-25
- 차트 제목 매핑 오류 수정 (컬럼명 추론 → Superset UI 차트 이름 매핑)
  - 원인: chart/data API는 SQL 컬럼명만 반환, 차트 제목(slice_name)은 dashboard/charts API에만 존재
  - 해결: slice_id → slice_name 매핑 구축 (dashboard/charts 응답 + URL form_data의 slice_id 활용)
  - shucle_api_probe.py 수정: 파일 저장 시 `_meta` 객체(slice_id, slice_name) 주입 + _summary.json에 slice_id/slice_name 추가
  - analyze_values.py 신규 생성: slice_map 기반 차트 제목 결정 (145개 차트 중 144개 매핑 성공)
- 전체 6개 탭 차트 데이터 완전 수집 성공 (이전: 3개 탭만 수집)
  - 원인: iframe 내 Superset 차트 데이터 로딩 시간 부족 + response.text() 프레임 교체 시 실패
  - 수정 1: response.body() (바이너리) 즉시 읽기로 변경 → 프레임 교체 전 확보
  - 수정 2: wait_for_chart_data() 스마트 대기 함수 — 응답 안정화 감지 (최대 60초, 8초간 새 응답 없으면 완료)
  - 수정 3: 차트 데이터 0건 시 재스크롤+재대기 로직
  - 결과: 266개 API (차트 185개) / 호출탑승 62, 서비스품질 25, 가호출수요 14, 지역회원 10, 정류장이용 11, 차량운행 23
  - analyze_data.py: 수집 데이터 테이블 출력 분석 스크립트 추가
- 커스텀 날짜 범위 지정 기능 추가
  - debug_datepicker.py로 date-range-picker 세그먼트 구조 분석
  - set_date_segment() 함수: spinbutton 세그먼트에 값 설정 (년도=숫자키, 월/일=ArrowUp/Down)
  - select_date_range() 확장: 프리셋("1주") + 커스텀 튜플(("2026-02-01", "2026-02-20")) 모두 지원
  - 커스텀 날짜 테스트 성공: 2026-02-01~2026-02-20 설정 → 186개 API 응답 수집
- 기간 설정 자동화 + 저장 경로 동적 생성 완료
  - select_date_range() 함수: date-range-picker__Shortcut 버튼 클릭 (1주/4주/12주)
  - get_save_dir() 함수: UI에서 지역명/날짜 읽어서 저장 경로 생성
  - inner_text 개행 이슈 해결 (날짜가 \n으로 분리됨)
  - 경로 형식: shucle_data/검단신도시/20260218_20260224/
- 지역 선택(검단신도시) 자동화 완료
  - debug_region.py로 UI 구조 분석: heroui 기반 커스텀 드롭다운, zone-select 컴포넌트
  - select_region() 함수 완전 재작성: aria-haspopup 버튼 탐색 → 검색창 입력 → zone-select__Value 클릭
  - 검단 데이터로 정상 수집 확인 (총 266개 API 응답)
- API 탐색 스크립트 실행 완료: 6개 탭에서 총 226개 API 응답 수집 (영덕군 데이터)
  - 원본 스크립트(c:\shucle_monitor\shucle_api_prob_260225.py)를 프로젝트 내로 복사 및 수정
  - 수정사항: input() 제거, UTF-8 인코딩 강제, 이모지→텍스트 태그, 지역선택 자동재시도+수동대기 폴백
  - 결과: shucle_data/api_probe/ 에 탭별 JSON 파일 저장
- 프로젝트 초기 설정: `shucle_monitor` 디렉토리 생성, git init, CLAUDE.md 생성
