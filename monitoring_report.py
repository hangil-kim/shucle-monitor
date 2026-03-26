"""셔클 DRT 모니터링 보고서 생성 스크립트
======================================
수집된 인사이트 데이터를 KPI 분석 체계에 따라 분석하고 보고서를 생성합니다.

분석 체계:
  수요 → 품질 → 공급 → 성장 (4개 카테고리)
  1차 핵심 지표 변동 시 → 2차 원인 분석 지표 드릴다운
  변동 감지 기준: 비교기간 대비 ±10%

사용법:
  python monitoring_report.py <데이터_디렉토리> [비교_디렉토리]

예시:
  python monitoring_report.py shucle_data/영덕관광/20260219_20260225
  python monitoring_report.py shucle_data/영덕관광/20260219_20260225 shucle_data/영덕관광/20260212_20260218
"""

import os, sys, json, re
from datetime import datetime, timezone
from collections import OrderedDict

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ANSI 색상 코드
RED = "\033[91m"
BLUE = "\033[94m"
RESET = "\033[0m"

# Windows ANSI 지원 활성화
if sys.platform == "win32":
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleMode(
            ctypes.windll.kernel32.GetStdHandle(-11), 7)
    except Exception:
        pass


# ================================================================
# 데이터 로딩
# ================================================================

def load_charts(data_dir):
    """데이터 디렉토리에서 차트 데이터 로드"""
    charts = []
    for fname in sorted(os.listdir(data_dir)):
        if not fname.endswith(".json") or fname.startswith("_") or fname.startswith("00"):
            continue
        fpath = os.path.join(data_dir, fname)
        with open(fpath, "r", encoding="utf-8") as f:
            try:
                data = json.load(f)
            except Exception:
                continue
        if not isinstance(data, dict):
            continue

        meta = data.get("_meta", {})
        slice_name = meta.get("slice_name", "")
        # ◼︎ 접두사 제거
        if slice_name:
            slice_name = re.sub(r"^[◼︎◻️▪▫●○■□\s]+", "", slice_name).strip()

        results = data.get("result", [])
        if not isinstance(results, list) or not results:
            continue
        first = results[0]
        if not isinstance(first, dict):
            continue

        # chart data만 로드 (colnames + data 있는 것)
        if "colnames" not in first or "data" not in first:
            continue

        colnames = first.get("colnames", [])
        coltypes = first.get("coltypes", [])
        rows = first.get("data", [])
        if not rows:
            continue

        # 멀티 result 병합
        for r in results[1:]:
            if isinstance(r, dict) and "data" in r:
                rows = rows + r["data"]

        charts.append({
            "filename": fname,
            "slice_id": meta.get("slice_id"),
            "slice_name": slice_name,
            "tab": meta.get("tab", ""),
            "colnames": colnames,
            "coltypes": coltypes,
            "rows": rows,
        })

    return charts


def find_chart(charts, includes, excludes=None, prefer_timeseries=True):
    """slice_name 패턴으로 차트 찾기 (시계열 차트 우선 매칭)"""
    matches = []
    for c in charts:
        name = c.get("slice_name", "")
        if not name:
            continue
        if all(p in name for p in includes):
            if excludes and any(e in name for e in excludes):
                continue
            matches.append(c)
    if not matches:
        return None
    if prefer_timeseries and len(matches) > 1:
        # coltypes에 2(timestamp)가 있는 차트 우선
        ts_matches = [c for c in matches if 2 in c.get("coltypes", [])]
        if ts_matches:
            return ts_matches[0]
    return matches[0]


def find_all_charts(charts, includes, excludes=None):
    """slice_name 패턴으로 매칭되는 모든 차트 찾기"""
    result = []
    for c in charts:
        name = c.get("slice_name", "")
        if not name:
            continue
        if all(p in name for p in includes):
            if excludes and any(e in name for e in excludes):
                continue
            result.append(c)
    return result


# ================================================================
# 데이터 추출
# ================================================================

def ts_to_date(ts):
    """밀리초 타임스탬프 → MM/DD 문자열"""
    if ts is None:
        return None
    try:
        dt = datetime.fromtimestamp(float(ts) / 1000, tz=timezone.utc)
        return dt.strftime("%m/%d")
    except Exception:
        return None


def find_ts_col(colnames, coltypes):
    """타임스탬프 컬럼 찾기"""
    for i, ct in enumerate(coltypes):
        if ct == 2:  # timestamp type
            return colnames[i]
    # fallback: __timestamp 이름으로 검색
    for cn in colnames:
        if "timestamp" in cn.lower() or "date" in cn.lower():
            return cn
    return None


def find_metric_cols(colnames, coltypes, col_filter=None):
    """숫자 메트릭 컬럼들 찾기"""
    ts_cols = {colnames[i] for i, ct in enumerate(coltypes) if ct == 2}
    metrics = []
    for i, cn in enumerate(colnames):
        if cn in ts_cols:
            continue
        if coltypes[i] == 0:  # numeric
            if col_filter:
                if col_filter in cn:
                    metrics.append(cn)
            else:
                metrics.append(cn)
    return metrics


def extract_daily(chart, col_filter=None):
    """
    차트에서 일별 시계열 추출.
    Returns: OrderedDict of date_str -> value (합산)
    """
    if not chart:
        return OrderedDict()
    colnames = chart.get("colnames", [])
    coltypes = chart.get("coltypes", [])
    rows = chart.get("rows", [])
    if not rows or not colnames:
        return OrderedDict()

    ts_col = find_ts_col(colnames, coltypes)
    if not ts_col:
        return OrderedDict()

    metric_cols = find_metric_cols(colnames, coltypes, col_filter)
    if not metric_cols:
        return OrderedDict()

    daily = OrderedDict()
    for row in rows:
        date = ts_to_date(row.get(ts_col))
        if not date:
            continue
        val = sum(row.get(c, 0) or 0 for c in metric_cols)
        daily[date] = daily.get(date, 0) + val

    return OrderedDict(sorted(daily.items()))


def extract_daily_cols(chart):
    """
    차트에서 일별 + 컬럼별 데이터 추출.
    Returns: OrderedDict of date_str -> {col_name: value}
    """
    if not chart:
        return OrderedDict(), []
    colnames = chart.get("colnames", [])
    coltypes = chart.get("coltypes", [])
    rows = chart.get("rows", [])
    if not rows:
        return OrderedDict(), []

    ts_col = find_ts_col(colnames, coltypes)
    if not ts_col:
        return OrderedDict(), []

    metric_cols = find_metric_cols(colnames, coltypes)
    daily = OrderedDict()
    for row in rows:
        date = ts_to_date(row.get(ts_col))
        if not date:
            continue
        vals = {c: row.get(c, 0) or 0 for c in metric_cols}
        daily[date] = vals

    return OrderedDict(sorted(daily.items())), metric_cols


def summarize(daily, agg="sum", operating_days=None):
    """일별 데이터 요약 통계.
    operating_days: 전체 운행일수. 지정 시 데이터 없는 운행일도 0으로 포함하여 평균 계산."""
    if not daily:
        return {"total": 0, "avg": 0, "min": 0, "max": 0, "count": 0, "daily": daily}
    vals = list(daily.values())
    total = sum(vals)
    divisor = operating_days if operating_days and operating_days >= len(vals) else len(vals)
    avg = total / divisor
    return {
        "total": total,
        "avg": avg,
        "min": min(vals),
        "max": max(vals),
        "count": len(vals),
        "daily": daily,
    }


def _parse_date(date_str):
    """다양한 날짜 형식 파싱 → datetime 객체. MM/DD 형식은 현재 연도 사용."""
    for fmt in ("%Y-%m-%d", "%m/%d", "%m-%d"):
        try:
            dt = datetime.strptime(date_str, fmt)
            if dt.year == 1900:  # MM/DD 파싱 시 기본 연도
                dt = dt.replace(year=datetime.now().year)
            return dt
        except ValueError:
            continue
    return None


def filter_daily_by_daytype(daily, daytype):
    """일별 데이터를 평일/주말로 필터링.
    daytype: "weekday" (월~금) 또는 "weekend" (토~일)
    Returns: 필터링된 OrderedDict
    """
    if not daily:
        return OrderedDict()
    filtered = OrderedDict()
    for date_str, val in daily.items():
        dt = _parse_date(date_str)
        if dt is None:
            continue
        is_weekend = dt.weekday() >= 5  # 5=토, 6=일
        if daytype == "weekend" and is_weekend:
            filtered[date_str] = val
        elif daytype == "weekday" and not is_weekend:
            filtered[date_str] = val
    return filtered


# ================================================================
# KPI 분석 체계 정의
# ================================================================

FRAMEWORK = [
    {
        "id": "demand",
        "category": "수요",
        "description": "얼마나 많이 이용하는가",
        "primary": [
            {
                "name": "실시간 호출 건수(일평균)",
                "unit": "건",
                "why": "전체 서비스 이용량의 기본 척도",
                "match": {"includes": ["실시간 호출"], "excludes": ["결과", "순 회원", "이동완료", "누적", "주간", "요일", "시간대", "연령"]},
                "agg": "sum",
                "triggers": [
                    {
                        "desc": "감소 (비교기간 대비 -10% 이상)",
                        "threshold": -0.10,
                        "drilldowns": [
                            {"name": "가호출 수(일평균)", "match": {"includes": ["가호출 수"], "excludes": ["순 회원", "성공", "결과", "성공률", "일별"]}, "reason": "가호출 대비 실호출 전환 저조"},
                            {"name": "배차실패 건수(일평균)", "match": {"includes": ["일별", "실시간 호출 결과"], "excludes": ["요일", "시간대"]}, "col": "배차실패", "reason": "배차 매칭 실패로 호출 유실"},
                            {"name": "호출취소 건수(일평균)", "match": {"includes": ["일별", "실시간 호출 결과"], "excludes": ["요일", "시간대"]}, "col": "호출취소", "reason": "이용자 자발적 취소 증가"},
                            {"name": "운행차량 대수", "match": {"includes": ["운행차량 대수"]}, "reason": "차량 공급 부족에 따른 수요 억제"},
                            {"name": "전화 호출 비율", "match": {"includes": ["호출 방식별 실시간 호출"]}, "cat_ratio": "전화", "is_pct": True, "reason": "전화 호출 채널 비중 변화"},
                            {"name": "앱 호출 비율", "match": {"includes": ["호출 방식별 실시간 호출"]}, "cat_ratio": "앱", "is_pct": True, "reason": "앱 호출 채널 비중 변화"},
                            {"name": "현장 호출 비율", "match": {"includes": ["호출 방식별 실시간 호출"]}, "cat_ratio": ["드라이버", None], "is_pct": True, "reason": "드라이버(현장) 호출 비중 변화"},
                        ],
                    },
                    {
                        "desc": "증가 (비교기간 대비 +10% 이상)",
                        "threshold": 0.10,
                        "drilldowns": [
                            {"name": "이동완료 호출 건수(일평균)", "match": {"includes": ["이동완료된 실시간 호출"]}, "reason": "호출 증가의 실질 완료 전환 정도"},
                            {"name": "평균 대기시간", "match": {"includes": ["평균 대기시간"], "excludes": ["시간대", "상위"]}, "reason": "수요 급증에 따른 품질 저하 우려"},
                            {"name": "전화 호출 비율", "match": {"includes": ["호출 방식별 실시간 호출"]}, "cat_ratio": "전화", "is_pct": True, "reason": "전화 호출 채널 비중 변화"},
                            {"name": "앱 호출 비율", "match": {"includes": ["호출 방식별 실시간 호출"]}, "cat_ratio": "앱", "is_pct": True, "reason": "앱 호출 채널 비중 변화"},
                            {"name": "현장 호출 비율", "match": {"includes": ["호출 방식별 실시간 호출"]}, "cat_ratio": ["드라이버", None], "is_pct": True, "reason": "드라이버(현장) 호출 비중 변화"},
                        ],
                    },
                ],
            },
            {
                "name": "이동완료 호출 건수(일평균)",
                "unit": "건",
                "why": "실제로 서비스가 완료된 건수 (핵심 성과)",
                "match": {"includes": ["이동완료된 실시간 호출"]},
                "agg": "sum",
                "triggers": [
                    {
                        "desc": "변동 (비교기간 대비 ±10% 이상)",
                        "threshold": 0.10,
                        "bidirectional": True,
                        "drilldowns": [
                            {"name": "미탑승 건수(일평균)", "match": {"includes": ["일별", "실시간 호출 결과"], "excludes": ["요일", "시간대"]}, "col": "미탑승", "reason": "배차 후 미탑승(노쇼) 발생"},
                            {"name": "호출취소 건수(일평균)", "match": {"includes": ["실시간 호출 결과"], "excludes": ["요일", "시간대"]}, "col": "호출취소", "reason": "이용자 자발적 취소"},
                            {"name": "배차실패 건수(일평균)", "match": {"includes": ["실시간 호출 결과"], "excludes": ["요일", "시간대"]}, "col": "배차실패", "reason": "배차 매칭 실패"},
                        ],
                    }
                ],
            },
            {
                "name": "총 탑승객 수(일평균)",
                "unit": "명",
                "why": "동승 포함 실 이용 인원 (수요 규모 파악)",
                "match": {"includes": ["총 탑승객"]},
                "agg": "sum",
                "triggers": [
                    {
                        "desc": "변동 (비교기간 대비 ±10% 이상)",
                        "threshold": 0.10,
                        "bidirectional": True,
                        "drilldowns": [
                            {"name": "동승 인원 분포(일평균)", "match": {"includes": ["동승 인원"], "excludes": ["시간대"]}, "reason": "동승 비율 변화에 따른 탑승객 증감"},
                            {"name": "고령자(60+) 호출 비율", "match": {"includes": ["연령대별 실시간 호출"]}, "cat_ratio": ["60대", "70대", "80대", "90대"], "is_pct": True, "reason": "고령 이용자 호출 비중 변화"},
                            {"name": "성인(20~50대) 호출 비율", "match": {"includes": ["연령대별 실시간 호출"]}, "cat_ratio": ["20대", "30대", "40대", "50대"], "is_pct": True, "reason": "성인 이용자 호출 비중 변화"},
                            {"name": "어린이/청소년 호출 비율", "match": {"includes": ["연령대별 실시간 호출"]}, "cat_ratio": ["0대", "10대"], "is_pct": True, "reason": "어린이/청소년 호출 비중 변화"},
                        ],
                    }
                ],
            },
            {
                "name": "대당 탑승객 수(일평균)",
                "unit": "명",
                "why": "차량당 실질 수요 효율",
                "match": {"includes": ["차량 대당 탑승객"]},
                "agg": "avg",
                "triggers": [
                    {
                        "desc": "변동 (비교기간 대비 ±10% 이상)",
                        "threshold": 0.10,
                        "bidirectional": True,
                        "drilldowns": [
                            {"name": "대당 탑승객 수(평일)", "match": {"includes": ["차량 대당 탑승객"]}, "weekday_filter": "weekday", "reason": "평일 차량당 탑승객 변화"},
                            {"name": "대당 탑승객 수(주말)", "match": {"includes": ["차량 대당 탑승객"]}, "weekday_filter": "weekend", "reason": "주말 차량당 탑승객 변화"},
                            {"name": "동승 인원 분포(일평균)", "match": {"includes": ["동승 인원"], "excludes": ["시간대"]}, "reason": "동승 비율 변화에 따른 대당 탑승 변화"},
                            {"name": "실시간 호출 건수(일평균)", "match": {"includes": ["실시간 호출"], "excludes": ["결과", "순 회원", "이동완료", "누적", "요일", "시간대", "연령"]}, "reason": "수요 변동에 따른 대당 배분 변화"},
                            {"name": "운행차량 대수", "match": {"includes": ["운행차량 대수"]}, "reason": "차량 수 변동에 따른 대당 배분 변화"},
                        ],
                    }
                ],
            },
        ],
    },
    {
        "id": "quality",
        "category": "품질",
        "description": "서비스 경험이 좋은가",
        "primary": [
            {
                "name": "평균 대기시간",
                "unit": "분",
                "why": "이용자 만족도에 가장 직접적 영향",
                "match": {"includes": ["평균 대기시간"], "excludes": ["시간대", "상위", "일별"]},
                "agg": "avg",
                "triggers": [
                    {
                        "desc": "증가 (비교기간 대비 +10% 이상)",
                        "threshold": 0.10,
                        "drilldowns": [
                            {"name": "평균 대기시간(평일)", "match": {"includes": ["평균 대기시간"], "excludes": ["시간대", "상위", "일별"]}, "weekday_filter": "weekday", "reason": "평일 대기시간 변화"},
                            {"name": "평균 대기시간(주말)", "match": {"includes": ["평균 대기시간"], "excludes": ["시간대", "상위", "일별"]}, "weekday_filter": "weekend", "reason": "주말 대기시간 변화"},
                            {"name": "상위 10% 대기시간", "match": {"includes": ["상위10% 대기시간"]}, "reason": "극단적 장시간 대기 사례 발생"},
                            {"name": "장시간 대기(30분+) 비율", "match": {"includes": ["대기시간 분포"], "excludes": ["시간대"]}, "long_wait_ratio": True, "wait_threshold_min": 30, "is_pct": True, "reason": "30분 이상 장시간 대기 비중"},
                            {"name": "운행차량 대수", "match": {"includes": ["운행차량 대수"]}, "reason": "차량 공급 부족에 따른 대기 증가"},
                            {"name": "실시간 호출 건수(일평균)", "match": {"includes": ["실시간 호출"], "excludes": ["결과", "순 회원", "이동완료", "누적", "요일", "시간대"]}, "reason": "수요 대비 공급 불균형"},
                        ],
                    }
                ],
            },
            {
                "name": "평균 우회비율",
                "unit": "배",
                "why": "경로 효율성 (1.0 = 직선, 높을수록 우회)",
                "match": {"includes": ["평균 우회"], "excludes": ["시간대", "상위", "추이", "일별"]},
                "agg": "avg",
                "triggers": [
                    {
                        "desc": "증가 (비교기간 대비 +10% 이상)",
                        "threshold": 0.10,
                        "drilldowns": [
                            {"name": "상위 10% 우회비율", "match": {"includes": ["상위10% 우회비율"]}, "reason": "극단적 우회 경로 발생"},
                            {"name": "평균 이동시간", "match": {"includes": ["이동시간"], "excludes": ["시간대"]}, "col": "평균 이동시간", "reason": "우회 경로 증가가 이동시간 상승 유발"},
                            {"name": "동승 인원 분포(일평균)", "match": {"includes": ["동승 인원"], "excludes": ["시간대"]}, "reason": "동승 증가로 경유지 추가"},
                        ],
                    }
                ],
            },
            {
                "name": "평균 이동시간",
                "unit": "분",
                "why": "실제 탑승~하차까지 소요시간",
                "match": {"includes": ["일별 이동시간"]},
                "col": "평균 이동시간",
                "agg": "avg",
                "triggers": [
                    {
                        "desc": "변동 (비교기간 대비 ±10% 이상)",
                        "threshold": 0.10,
                        "bidirectional": True,
                        "drilldowns": [
                            {"name": "피크 시간대 이동시간", "match": {"includes": ["시간대별 이동시간"]}, "peak_travel": True, "reason": "피크 시간대 이동시간 변화"},
                            {"name": "비피크 시간대 이동시간", "match": {"includes": ["시간대별 이동시간"]}, "offpeak_travel": True, "reason": "비피크 시간대 이동시간 변화"},
                            {"name": "평균 우회비율", "match": {"includes": ["평균 우회"], "excludes": ["시간대", "상위", "추이"]}, "reason": "경로 우회에 따른 이동시간 증가"},
                        ],
                    }
                ],
            },
        ],
    },
    {
        "id": "supply",
        "category": "공급",
        "description": "차량이 충분히 운영되는가",
        "primary": [
            {
                "name": "운행차량 대수",
                "unit": "대",
                "why": "서비스 공급 역량의 기본 척도",
                "match": {"includes": ["운행차량 대수"]},
                "agg": "avg",
                "triggers": [
                    {
                        "desc": "변동 (비교기간 대비 ±10% 이상)",
                        "threshold": 0.10,
                        "bidirectional": True,
                        "drilldowns": [
                            {"name": "일별 운행 차량 수", "match": {"includes": ["일별 운행 차량"]}, "reason": "특정일 차량 미운행"},
                            {"name": "근무 이행률", "match": {"includes": ["근무 이행률"], "excludes": ["운행"]}, "reason": "기사 근무 미이행"},
                            {"name": "배차실패 건수(일평균)", "match": {"includes": ["실시간 호출 결과"], "excludes": ["요일", "시간대"]}, "col": "배차실패", "reason": "차량 감소에 따른 배차 실패 연쇄"},
                        ],
                    }
                ],
            },
            {
                "name": "대당 운행시간(일평균)",
                "unit": "시간",
                "why": "차량당 실제 운행 효율",
                "match": {"includes": ["평균 대당 운행시간"]},
                "agg": "avg",
                "triggers": [
                    {
                        "desc": "변동 (비교기간 대비 ±10% 이상)",
                        "threshold": 0.10,
                        "bidirectional": True,
                        "drilldowns": [
                            {"name": "차량별 운행 시간", "match": {"includes": ["차량별", "운행 시간"], "excludes": ["근무"]}, "reason": "특정 차량 운행 시간 편차"},
                            {"name": "평균 근무시간", "match": {"includes": ["평균 근무시간"]}, "reason": "공차 대기 시간 증가"},
                            {"name": "차량 대당 탑승객", "match": {"includes": ["차량 대당 탑승객"]}, "reason": "수요 감소에 따른 공회전"},
                        ],
                    }
                ],
            },
            {
                "name": "가호출 성공률",
                "unit": "%",
                "why": "수요 대비 공급 매칭 효율",
                "match": {"includes": ["가호출 성공률"], "excludes": ["세션", "시간대", "일별"]},
                "agg": "avg",
                "is_pct": True,
                "triggers": [
                    {
                        "desc": "하락 (비교기간 대비 -10%p 이상)",
                        "threshold": -0.10,
                        "drilldowns": [
                            {"name": "가호출 성공률(평일)", "match": {"includes": ["가호출 성공률"], "excludes": ["세션", "시간대", "일별"]}, "weekday_filter": "weekday", "is_pct": True, "reason": "평일 가호출 성공률 변화"},
                            {"name": "가호출 성공률(주말)", "match": {"includes": ["가호출 성공률"], "excludes": ["세션", "시간대", "일별"]}, "weekday_filter": "weekend", "is_pct": True, "reason": "주말 가호출 성공률 변화"},
                            {"name": "피크 시간대 가호출 성공률", "match": {"includes": ["시간대별 가호출 성공률"]}, "peak_success": True, "is_pct": True, "reason": "피크 시간대 가호출 성공률 변화"},
                            {"name": "가호출 성공/실패 회원(일평균)", "match": {"includes": ["가호출 성공", "실패", "회원"], "excludes": ["시간대"]}, "reason": "가호출 실패 경험 이용자 증가"},
                            {"name": "운행차량 대수", "match": {"includes": ["운행차량 대수"]}, "reason": "차량 공급 부족에 따른 매칭 실패"},
                            {"name": "가호출 수(일평균)", "match": {"includes": ["가호출 수"], "excludes": ["순 회원", "성공", "결과", "성공률", "일별"]}, "reason": "수요 급증 대비 공급 부족"},
                        ],
                    }
                ],
            },
            {
                "name": "대당 운행거리(일평균)",
                "unit": "km",
                "why": "차량당 실제 이동 거리 (공급 활용도)",
                "match": {"includes": ["평균 대당 운행거리"], "excludes": ["월간", "주간"]},
                "agg": "avg",
                "triggers": [
                    {
                        "desc": "변동 (비교기간 대비 ±10% 이상)",
                        "threshold": 0.10,
                        "bidirectional": True,
                        "drilldowns": [
                            {"name": "일간 평균 대당 운행거리", "match": {"includes": ["일간 평균 대당 운행거리"]}, "reason": "일별 운행거리 편차"},
                            {"name": "실시간 호출 건수(일평균)", "match": {"includes": ["실시간 호출"], "excludes": ["결과", "순 회원", "이동완료", "누적", "요일", "시간대", "연령"]}, "reason": "수요 변동에 따른 운행거리 변화"},
                            {"name": "운행차량 대수", "match": {"includes": ["운행차량 대수"]}, "reason": "차량 수 변동에 따른 대당 거리 변화"},
                        ],
                    }
                ],
            },
            {
                "name": "평균 경로이탈비중",
                "unit": "%",
                "why": "내비게이션 경로 준수율 (운행 품질)",
                "match": {"includes": ["경로 준수"]},
                "flat_col": "경로이탈비중",
                "agg": "avg",
                "is_pct": True,
                "triggers": [
                    {
                        "desc": "증가 (비교기간 대비 +10% 이상)",
                        "threshold": 0.10,
                        "drilldowns": [
                            {"name": "평균 우회비율", "match": {"includes": ["평균 우회"], "excludes": ["시간대", "상위", "추이", "일별"]}, "reason": "경로 이탈이 우회비율 증가 유발"},
                            {"name": "평균 이동시간", "match": {"includes": ["일별 이동시간"]}, "col": "평균 이동시간", "reason": "경로 이탈이 이동시간 증가 유발"},
                        ],
                    }
                ],
            },
        ],
    },
    {
        "id": "growth",
        "category": "성장",
        "description": "이용자가 늘고 있는가",
        "primary": [
            {
                "name": "DAU (일간 활성 회원)",
                "unit": "명",
                "why": "일상적 이용 빈도 파악",
                "match": {"includes": ["일간 활성 지역 회원"]},
                "agg": "avg",
                "triggers": [
                    {
                        "desc": "변동 (비교기간 대비 ±10% 이상)",
                        "threshold": 0.10,
                        "bidirectional": True,
                        "drilldowns": [
                            {"name": "WAU (주간 활성 회원)", "match": {"includes": ["주간 활성"]}, "reason": "주간 활성도 추이 연동"},
                            {"name": "신규 지역 회원(일평균)", "match": {"includes": ["신규 지역 회원"], "excludes": ["일별"]}, "reason": "신규 유입 증감 영향"},
                            {"name": "활성 회원 평균연령", "match": {"includes": ["활성 지역 회원 연령대"]}, "age_avg": True, "unit": "세", "reason": "이용자 평균 연령 변화"},
                            {"name": "가호출 성공률", "match": {"includes": ["가호출 성공률"], "excludes": ["세션", "시간대"]}, "is_pct": True, "reason": "서비스 품질 불만에 따른 이탈"},
                            {"name": "평균 대기시간", "match": {"includes": ["평균 대기시간"], "excludes": ["시간대", "상위"]}, "reason": "대기시간 증가에 따른 이탈"},
                        ],
                    }
                ],
            },
            {
                "name": "신규 지역 회원(일평균)",
                "unit": "명",
                "why": "서비스 성장세 파악",
                "match": {"includes": ["신규 지역 회원"], "excludes": ["일별"]},
                "agg": "sum",
                "triggers": [
                    {
                        "desc": "변동 (비교기간 대비 ±10% 이상)",
                        "threshold": 0.10,
                        "bidirectional": True,
                        "drilldowns": [
                            {"name": "누적 지역 회원(일평균)", "match": {"includes": ["누적 지역 회원"], "excludes": ["일별"]}, "reason": "전체 회원 기반 성장세"},
                            {"name": "가호출 순 회원(일평균)", "match": {"includes": ["가호출 순 회원"]}, "reason": "가입 후 미이용(잠재 이탈) 회원"},
                        ],
                    }
                ],
            },
        ],
    },
]


# ================================================================
# 보고서 생성
# ================================================================

def parse_dir_info(data_dir):
    """디렉토리 경로에서 지역명, 기간 파싱"""
    parts = os.path.normpath(data_dir).replace("\\", "/").split("/")
    region = "알 수 없음"
    period = "알 수 없음"
    for i, p in enumerate(parts):
        if p == "shucle_data" and i + 2 < len(parts):
            region = parts[i + 1]
            period = parts[i + 2]
            break
    # 기간 포매팅: 20260219_20260225 → 2026.02.19 ~ 02.25
    m = re.match(r"(\d{4})(\d{2})(\d{2})_(\d{4})(\d{2})(\d{2})", period)
    if m:
        y1, m1, d1, y2, m2, d2 = m.groups()
        period_fmt = f"{y1}.{m1}.{d1} ~ {m2}.{d2}"
    else:
        period_fmt = period
    return region, period_fmt


def get_operating_days(charts):
    """운행차량 대수 차트에서 실제 운행일수(차량 > 0인 날)를 반환."""
    chart = find_chart(charts, includes=["운행차량 대수"])
    if not chart:
        return None
    daily = extract_daily(chart)
    if not daily:
        return None
    return sum(1 for v in daily.values() if v and v > 0)


def fmt_val(val, unit="", is_pct=False):
    """값 포매팅 (소수점 첫째자리까지)"""
    if val is None:
        return "-"
    if is_pct:
        return f"{val * 100:.1f}%"
    if isinstance(val, float):
        if abs(val) >= 100:
            return f"{val:,.0f}{unit}"
        else:
            return f"{val:.1f}{unit}"
    return f"{val:,}{unit}"


def fmt_daily(daily, unit="", max_items=7, is_pct=False):
    """일별 데이터를 한 줄 문자열로"""
    items = list(daily.items())[:max_items]
    parts = [f"{d}={fmt_val(v, unit, is_pct)}" for d, v in items]
    return " | ".join(parts)


def extract_flat_avg(chart, col_name):
    """타임스탬프 없는 차트에서 특정 컬럼의 평균값 추출"""
    if not chart:
        return None
    rows = chart.get("rows", [])
    if not rows:
        return None
    vals = []
    for row in rows:
        v = row.get(col_name)
        if v is not None and isinstance(v, (int, float)):
            vals.append(v)
    if not vals:
        return None
    return sum(vals) / len(vals)


def get_kpi_value(charts, kpi_def, operating_days=None):
    """KPI 정의에서 현재 값 추출.
    operating_days: 전체 운행일수. 데이터 없는 운행일도 0으로 포함하여 일평균 계산."""
    match = kpi_def["match"]
    chart = find_chart(charts, **match)
    if not chart:
        return None, OrderedDict(), None

    # flat_col: 타임스탬프 없는 차트의 특정 컬럼 직접 추출
    flat_col = kpi_def.get("flat_col")
    if flat_col:
        value = extract_flat_avg(chart, flat_col)
        return value, OrderedDict(), chart

    col_filter = kpi_def.get("col")
    daily = extract_daily(chart, col_filter)
    if not daily:
        return None, OrderedDict(), chart

    stats = summarize(daily, kpi_def.get("agg", "sum"), operating_days)
    value = stats["avg"]  # 항상 일일 평균 (운행일수 기준)
    return value, daily, chart


def extract_categorical_sum(chart, col_filter=None):
    """카테고리형 차트(시계열 없음)에서 숫자 컬럼 합계 추출.
    Returns: {"total": X, "avg": Y, "count": N, "daily": OrderedDict()}
    """
    if not chart:
        return None
    colnames = chart.get("colnames", [])
    coltypes = chart.get("coltypes", [])
    rows = chart.get("rows", [])
    if not rows:
        return None

    metric_cols = find_metric_cols(colnames, coltypes, col_filter)
    if not metric_cols:
        return None

    total = 0
    count = len(rows)
    for row in rows:
        total += sum(row.get(c, 0) or 0 for c in metric_cols)

    if count == 0:
        return None
    return {"total": total, "avg": total / count, "min": 0, "max": 0, "count": count, "daily": OrderedDict()}


def extract_category_ratio(chart, category_value, category_col=None):
    """카테고리형 차트에서 특정 카테고리의 비율(0~1) 추출.
    category_value: 매칭할 카테고리 값
        - 단일 문자열: 정확히 일치하는 행
        - 리스트: 리스트 내 값 중 하나와 일치하는 행들의 합
        - None: null 행 매칭
    category_col: 카테고리 컬럼명 (None이면 coltypes==1인 첫 컬럼)
    """
    if not chart:
        return None
    colnames = chart.get("colnames", [])
    coltypes = chart.get("coltypes", [])
    rows = chart.get("rows", [])
    if not rows:
        return None

    # 카테고리 컬럼 찾기
    if not category_col:
        for i, ct in enumerate(coltypes):
            if ct == 1:  # string type
                category_col = colnames[i]
                break
    if not category_col:
        return None

    # 숫자 컬럼 찾기
    metric_cols = find_metric_cols(colnames, coltypes)
    if not metric_cols:
        return None

    # 전체 합계와 해당 카테고리 합계 계산
    grand_total = 0
    target_total = 0
    for row in rows:
        row_val = sum(row.get(c, 0) or 0 for c in metric_cols)
        grand_total += row_val
        cat_val = row.get(category_col)
        if category_value is None:
            if cat_val is None:
                target_total += row_val
        elif isinstance(category_value, list):
            if cat_val in category_value:
                target_total += row_val
        else:
            if cat_val == category_value:
                target_total += row_val

    if grand_total == 0:
        return None
    return target_total / grand_total


def extract_age_avg(chart, category_col=None):
    """카테고리형 연령대 차트에서 가중평균 연령 추출.
    'N대' 형식의 카테고리를 대표 연령(N+5)으로 변환 후 가중평균 계산.
    """
    if not chart:
        return None
    colnames = chart.get("colnames", [])
    coltypes = chart.get("coltypes", [])
    rows = chart.get("rows", [])
    if not rows:
        return None

    if not category_col:
        for i, ct in enumerate(coltypes):
            if ct == 1:
                category_col = colnames[i]
                break
    if not category_col:
        return None

    metric_cols = find_metric_cols(colnames, coltypes)
    if not metric_cols:
        return None

    total_weight = 0
    weighted_sum = 0
    for row in rows:
        cat_val = row.get(category_col, "")
        if not cat_val:
            continue
        # "N대" → 대표 연령 N+5 (예: "70대" → 75)
        m = re.match(r"(\d+)대", str(cat_val))
        if not m:
            continue
        rep_age = int(m.group(1)) + 5
        weight = sum(row.get(c, 0) or 0 for c in metric_cols)
        weighted_sum += rep_age * weight
        total_weight += weight

    if total_weight == 0:
        return None
    return weighted_sum / total_weight


def _find_hour_col(colnames):
    """시간대(hour) 컬럼 찾기 (컬럼명에 'hour' 포함)"""
    for cn in colnames:
        if "hour" in cn.lower():
            return cn
    return None


def _build_hour_volume(volume_charts):
    """볼륨 차트들에서 시간대별 총 볼륨 집계. Returns: dict {hour: volume}"""
    hour_volume = {}
    for chart in volume_charts:
        colnames = chart.get("colnames", [])
        coltypes = chart.get("coltypes", [])
        rows = chart.get("rows", [])
        if not rows:
            continue
        hour_col = _find_hour_col(colnames)
        if not hour_col:
            continue
        metric_cols = find_metric_cols(colnames, coltypes)
        if not metric_cols:
            continue
        for row in rows:
            h = row.get(hour_col)
            if h is None:
                continue
            h = int(h)
            vol = sum(row.get(c, 0) or 0 for c in metric_cols)
            hour_volume[h] = hour_volume.get(h, 0) + vol
    return hour_volume


def _find_best_consecutive_2h(hour_volume, mode="max"):
    """연속 2시간 중 합이 최대/최소인 구간 찾기.
    mode: "max" → 피크, "min" → 비피크
    Returns: (start, end) 예: (9, 11) → 9시~11시
    """
    if not hour_volume:
        return None, None

    sorted_hours = sorted(hour_volume.keys())
    candidates = []
    for h in sorted_hours:
        if h + 1 in hour_volume:
            candidates.append((h, hour_volume[h] + hour_volume[h + 1]))

    if not candidates:
        # 연속 구간 없으면 단일 최대/최소 시간
        if mode == "max":
            best = max(hour_volume, key=hour_volume.get)
        else:
            best = min(hour_volume, key=hour_volume.get)
        return best, best + 1

    if mode == "max":
        best_start = max(candidates, key=lambda x: x[1])[0]
    else:
        best_start = min(candidates, key=lambda x: x[1])[0]
    return best_start, best_start + 2


def _find_peak_hours(volume_charts):
    """볼륨 차트들에서 가장 호출이 집중되는 연속 2시간 탐지.
    Returns: (peak_start, peak_end) 예: (9, 11) → 9시~11시
    """
    hour_volume = _build_hour_volume(volume_charts)
    return _find_best_consecutive_2h(hour_volume, mode="max")


def _find_offpeak_hours(volume_charts):
    """볼륨 차트들에서 가장 호출이 적은 연속 2시간 탐지.
    Returns: (offpeak_start, offpeak_end)
    """
    hour_volume = _build_hour_volume(volume_charts)
    return _find_best_consecutive_2h(hour_volume, mode="min")


def extract_peak_success(charts):
    """피크 시간대 가호출 성공률 추출.
    1) 볼륨 차트("시간대별 실시간 호출 결과")에서 피크 2시간 탐지
    2) 성공률 차트("시간대별 가호출 성공률")에서 해당 시간대 성공률 평균 추출
    Returns: (success_rate, peak_start, peak_end) or (None, None, None)
    """
    # 볼륨 차트 찾기
    volume_charts = find_all_charts(charts,
        includes=["시간대별 실시간 호출 결과"],
        excludes=None)
    if not volume_charts:
        return None, None, None

    peak_start, peak_end = _find_peak_hours(volume_charts)
    if peak_start is None:
        return None, None, None

    return _extract_success_at(charts, peak_start, peak_end)


def _extract_success_at(charts, peak_start, peak_end):
    """주어진 피크 시간대의 가호출 성공률 평균 추출.
    Returns: (success_rate, peak_start, peak_end)
    """
    # 성공률 차트 찾기
    rate_charts = find_all_charts(charts,
        includes=["시간대별 가호출 성공률"],
        excludes=None)
    if not rate_charts:
        return None, peak_start, peak_end

    success_vals = []
    for chart in rate_charts:
        colnames = chart.get("colnames", [])
        rows = chart.get("rows", [])
        if not rows:
            continue
        hour_col = _find_hour_col(colnames)
        if not hour_col:
            continue
        # "가호출 성공" 컬럼 찾기
        success_col = None
        for cn in colnames:
            if "성공" in cn and "실패" not in cn:
                success_col = cn
                break
        if not success_col:
            continue

        for row in rows:
            h = row.get(hour_col)
            if h is None:
                continue
            h = int(h)
            if peak_start <= h < peak_end:
                v = row.get(success_col)
                if v is not None:
                    success_vals.append(float(v))

    if not success_vals:
        return None, peak_start, peak_end

    avg_rate = sum(success_vals) / len(success_vals)
    return avg_rate, peak_start, peak_end


def _extract_travel_at(charts, hour_start, hour_end):
    """주어진 시간대의 실제 이동시간 평균 추출.
    Returns: (avg_travel, hour_start, hour_end)
    """
    travel_charts = find_all_charts(charts,
        includes=["시간대별 이동시간"], excludes=None)
    if not travel_charts:
        return None, hour_start, hour_end

    vals = []
    for chart in travel_charts:
        colnames = chart.get("colnames", [])
        rows = chart.get("rows", [])
        if not rows:
            continue
        hour_col = _find_hour_col(colnames)
        if not hour_col:
            continue
        # "실제 이동시간" 컬럼 찾기
        travel_col = None
        for cn in colnames:
            if "실제" in cn and "이동" in cn:
                travel_col = cn
                break
        if not travel_col:
            continue

        for row in rows:
            h = row.get(hour_col)
            if h is None:
                continue
            h = int(h)
            if hour_start <= h < hour_end:
                v = row.get(travel_col)
                if v is not None:
                    vals.append(float(v))

    if not vals:
        return None, hour_start, hour_end

    return sum(vals) / len(vals), hour_start, hour_end


def _get_volume_charts(charts):
    """볼륨 차트 찾기 (시간대별 실시간 호출 결과)"""
    return find_all_charts(charts,
        includes=["시간대별 실시간 호출 결과"], excludes=None)


def extract_peak_travel(charts):
    """피크 시간대 이동시간 추출."""
    volume_charts = _get_volume_charts(charts)
    if not volume_charts:
        return None, None, None
    peak_start, peak_end = _find_peak_hours(volume_charts)
    if peak_start is None:
        return None, None, None
    return _extract_travel_at(charts, peak_start, peak_end)


def extract_offpeak_travel(charts):
    """비피크 시간대 이동시간 추출."""
    volume_charts = _get_volume_charts(charts)
    if not volume_charts:
        return None, None, None
    offpeak_start, offpeak_end = _find_offpeak_hours(volume_charts)
    if offpeak_start is None:
        return None, None, None
    return _extract_travel_at(charts, offpeak_start, offpeak_end)


def _extract_long_wait_ratio(chart, threshold_min=30):
    """대기시간 구간 분포 차트에서 장시간 대기(threshold_min 이상) 비율 추출.
    컬럼명 패턴: 'a.5분 미만', 'g.30-35분 미만', 'i.40분이상' 등
    각 컬럼의 하한값을 파싱하여 threshold 이상인 컬럼의 합 / 전체 합 = 비율
    """
    if not chart:
        return None
    colnames = chart.get("colnames", [])
    coltypes = chart.get("coltypes", [])
    rows = chart.get("rows", [])
    if not rows:
        return None

    # 숫자 메트릭 컬럼(대기시간 구간들)과 각 하한값 파싱
    metric_cols = find_metric_cols(colnames, coltypes)
    if not metric_cols:
        return None

    col_lower_bounds = {}
    for cn in metric_cols:
        # "40분이상" → 40, "30-35분 미만" → 30, "5분 미만" → 0 (5미만이므로 하한=0)
        m = re.search(r"(\d+)분이상", cn)
        if m:
            col_lower_bounds[cn] = int(m.group(1))
            continue
        m = re.search(r"(\d+)-(\d+)분", cn)
        if m:
            col_lower_bounds[cn] = int(m.group(1))
            continue
        m = re.search(r"(\d+)분 미만", cn)
        if m:
            col_lower_bounds[cn] = 0  # "5분 미만" → 하한 0
            continue

    if not col_lower_bounds:
        return None

    total = 0
    long_total = 0
    for row in rows:
        for cn, lower in col_lower_bounds.items():
            v = row.get(cn) or 0
            total += v
            if lower >= threshold_min:
                long_total += v

    if total == 0:
        return None
    return long_total / total


def _resolve_dynamic_name(dd, dd_stats):
    """드릴다운 동적 이름 생성 (피크/비피크 시간대 등)"""
    if dd_stats and "peak_start" in dd_stats:
        ps, pe = dd_stats["peak_start"], dd_stats["peak_end"]
        if dd.get("peak_success"):
            return f"피크 시간대({ps}~{pe}시) 가호출 성공률"
        if dd.get("peak_travel"):
            return f"피크 시간대({ps}~{pe}시) 이동시간"
        if dd.get("offpeak_travel"):
            return f"비피크 시간대({ps}~{pe}시) 이동시간"
    return dd["name"]


def get_drilldown_value(charts, dd_def, operating_days=None):
    """드릴다운 지표 값 추출 (여러 매칭 차트 중 데이터 있는 것 사용)"""
    match = dd_def["match"]
    all_matched = find_all_charts(charts, **match)
    col_filter = dd_def.get("col")

    # weekday_filter: 평일/주말 필터링 모드
    daytype = dd_def.get("weekday_filter")
    if daytype:
        for chart in all_matched:
            daily = extract_daily(chart, col_filter)
            if daily:
                filtered = filter_daily_by_daytype(daily, daytype)
                if filtered:
                    stats = summarize(filtered, operating_days=operating_days)
                    return stats, filtered
        return None, OrderedDict()

    # age_avg: 연령대 가중평균 추출 모드
    if dd_def.get("age_avg"):
        for chart in all_matched:
            avg_age = extract_age_avg(chart)
            if avg_age is not None:
                stats = {"total": avg_age, "avg": avg_age, "min": avg_age, "max": avg_age, "count": 1, "daily": OrderedDict()}
                return stats, OrderedDict()
        return None, OrderedDict()

    # peak_success: 피크 시간대 가호출 성공률 추출 모드
    if dd_def.get("peak_success"):
        # _peak_hours가 있으면 비교기간용 (분석기간에서 결정한 피크 시간 재사용)
        forced = dd_def.get("_peak_hours")
        if forced:
            rate, ps, pe = _extract_success_at(charts, forced[0], forced[1])
        else:
            rate, ps, pe = extract_peak_success(charts)
            if ps is not None:
                dd_def["_peak_hours"] = (ps, pe)
        if rate is not None:
            stats = {"total": rate, "avg": rate, "min": rate, "max": rate,
                     "count": 1, "daily": OrderedDict(),
                     "peak_start": ps, "peak_end": pe}
            return stats, OrderedDict()
        return None, OrderedDict()

    # peak_travel / offpeak_travel: 피크/비피크 시간대 이동시간 추출 모드
    if dd_def.get("peak_travel") or dd_def.get("offpeak_travel"):
        is_peak = dd_def.get("peak_travel", False)
        hours_key = "_peak_travel_hours" if is_peak else "_offpeak_travel_hours"
        forced = dd_def.get(hours_key)
        if forced:
            val, hs, he = _extract_travel_at(charts, forced[0], forced[1])
        else:
            if is_peak:
                val, hs, he = extract_peak_travel(charts)
            else:
                val, hs, he = extract_offpeak_travel(charts)
            if hs is not None:
                dd_def[hours_key] = (hs, he)
        if val is not None:
            stats = {"total": val, "avg": val, "min": val, "max": val,
                     "count": 1, "daily": OrderedDict(),
                     "peak_start": hs, "peak_end": he}
            return stats, OrderedDict()
        return None, OrderedDict()

    # long_wait_ratio: 장시간 대기 비율 추출 모드
    if dd_def.get("long_wait_ratio"):
        threshold = dd_def.get("wait_threshold_min", 30)
        for chart in all_matched:
            ratio = _extract_long_wait_ratio(chart, threshold)
            if ratio is not None:
                stats = {"total": ratio, "avg": ratio, "min": ratio, "max": ratio,
                         "count": 1, "daily": OrderedDict()}
                return stats, OrderedDict()
        return None, OrderedDict()

    # cat_ratio: 카테고리 비율 추출 모드 (값이 None일 수 있으므로 키 존재 여부로 판별)
    if "cat_ratio" in dd_def:
        cat_ratio = dd_def["cat_ratio"]
        for chart in all_matched:
            ratio = extract_category_ratio(chart, cat_ratio)
            if ratio is not None:
                stats = {"total": ratio, "avg": ratio, "min": ratio, "max": ratio, "count": 1, "daily": OrderedDict()}
                return stats, OrderedDict()
        return None, OrderedDict()

    # 1차: 시계열 데이터 시도
    for chart in all_matched:
        daily = extract_daily(chart, col_filter)
        if daily:
            stats = summarize(daily, operating_days=operating_days)
            return stats, daily

    # 2차: 카테고리형 데이터 시도 (시계열 없는 차트)
    for chart in all_matched:
        stats = extract_categorical_sum(chart, col_filter)
        if stats:
            return stats, OrderedDict()

    return None, OrderedDict()


def dynamic_point(kpi_name, kpi_change, dd_name, dd_change, dd_curr, dd_prev, dd_is_pct=False):
    """2차 세부지표 동적 포인트 생성 (~20자 이내).
    1차 핵심지표와 연결하여 의미적 해석.
    """
    # ── 데이터 부족 ──
    if dd_curr is None and dd_prev is None:
        return "데이터 없음"
    if dd_prev is None and dd_curr is not None:
        return "비교기간 데이터 없음"
    if dd_curr is None and dd_prev is not None:
        return "분석기간 데이터 없음"

    # ── 0→0 변동 없음 ──
    if dd_change is None:
        if dd_curr == 0 and dd_prev == 0:
            # 건수 지표 0→0
            if "건수" in dd_name:
                return "발생 없음"
            return "변동 없음"
        return "변동률 산출 불가"

    # ── inf (0→N 또는 N→0) ──
    if dd_change == float("inf"):
        if "건수" in dd_name:
            return f"미발생→{dd_curr:.0f}건 신규 발생"
        return "신규 발생"
    if dd_change == float("-inf"):
        if "건수" in dd_name:
            return "완전 해소 (0건)"
        return "완전 해소"

    dd_dir = "증가" if dd_change > 0 else "감소"
    dd_pct = f"{abs(dd_change)*100:.0f}%"
    kpi_dir = "증가" if (kpi_change and kpi_change > 0) else "감소"

    # ── 특정 지표별 의미적 해석 ──

    # 운행차량 대수: 절대값 기반
    if "운행차량" in dd_name:
        if abs(dd_change) < 0.03:
            if dd_curr is not None and dd_curr <= 2:
                return f"{dd_curr:.0f}대 유지, 증차 여지 없음"
            return f"{dd_curr:.0f}대 유지, 공급 변동 없음"
        return f"{dd_prev:.0f}→{dd_curr:.0f}대, 공급 {dd_dir}"

    # 배차실패/호출취소/미탑승 건수: 절대값 + 맥락
    if any(k in dd_name for k in ["배차실패", "호출취소", "미탑승"]):
        event = dd_name.split("(")[0].strip()
        if dd_curr == 0 and dd_prev == 0:
            return f"{event} 없음"
        if dd_curr == 0:
            return f"{event} 해소"
        if dd_prev == 0:
            return f"{event} 신규 발생"
        if abs(dd_change) < 0.05:
            return f"{event} 유지"
        return f"{event} {dd_pct} {dd_dir}"

    # 가호출 수: 실호출과의 관계
    if "가호출 수" in dd_name:
        if abs(dd_change) < 0.05:
            return "가호출 안정"
        if kpi_change and kpi_change < -0.1 and dd_change > 0.1:
            return "가호출 증가, 실호출 미전환"
        if kpi_change and kpi_change < -0.1 and dd_change < -0.1:
            return "가호출/실호출 동반 감소"
        if kpi_change and kpi_change > 0.1 and dd_change > 0.1:
            return "가호출 증가, 수요 견인"
        return f"가호출 {dd_pct} {dd_dir}"

    # 대기시간 관련: 절대값 임계치
    if "대기시간" in dd_name:
        if "상위" in dd_name or "10%" in dd_name:
            if dd_curr is not None and dd_curr >= 10:
                return f"상위10% {dd_curr:.0f}분, 장시간 대기"
            if abs(dd_change) < 0.05:
                return f"극단 대기 {dd_curr:.1f}분 유지"
            return f"극단 대기 {dd_pct} {dd_dir}"
        if "평일" in dd_name or "주말" in dd_name:
            daytype = "평일" if "평일" in dd_name else "주말"
            if abs(dd_change) < 0.05:
                return f"{daytype} {dd_curr:.1f}분 안정"
            return f"{daytype} {dd_curr:.1f}분 ({dd_pct} {dd_dir})"
        if abs(dd_change) < 0.05:
            return f"{dd_curr:.1f}분 안정"
        return f"{dd_prev:.1f}→{dd_curr:.1f}분 {dd_dir}"

    # 이동시간
    if "이동시간" in dd_name:
        if abs(dd_change) < 0.05:
            return f"{dd_curr:.1f}분 안정"
        return f"{dd_prev:.1f}→{dd_curr:.1f}분 {dd_dir}"

    # 우회비율
    if "우회비율" in dd_name:
        if abs(dd_change) < 0.05:
            return f"{dd_curr:.1f}배 안정"
        if dd_curr is not None and dd_curr > 1.5:
            return f"{dd_curr:.1f}배, 우회 심화"
        return f"{dd_prev:.1f}→{dd_curr:.1f}배 {dd_dir}"

    # 가호출 성공률: 임계치 기반
    if "성공률" in dd_name:
        if "평일" in dd_name or "주말" in dd_name:
            daytype = "평일" if "평일" in dd_name else "주말"
            if dd_curr is not None:
                return f"{daytype} {dd_curr*100:.0f}% ({dd_pct} {dd_dir})"
        if "피크" in dd_name:
            if dd_curr is not None and dd_curr < 0.5:
                return f"피크 {dd_curr*100:.0f}%, 매칭 부족"
            if dd_curr is not None:
                return f"피크 {dd_curr*100:.0f}% ({dd_pct} {dd_dir})"
        if dd_curr is not None and dd_curr < 0.3:
            return f"{dd_curr*100:.0f}%, 매칭 심각 부족"
        if dd_curr is not None and dd_curr < 0.5:
            return f"{dd_curr*100:.0f}%, 매칭 부족"

    # 호출 비율 (전화/앱/현장): 비중 변화 해석
    if "호출 비율" in dd_name:
        channel = dd_name.replace(" 호출 비율", "")
        if abs(dd_change) < 0.05:
            return f"{channel} 비중 유지"
        if dd_curr is not None and dd_curr > 0.5:
            return f"{channel} 비중 {dd_curr*100:.0f}%로 주도적"
        if dd_curr is not None:
            return f"{channel} {dd_prev*100:.0f}→{dd_curr*100:.0f}%"
        return f"{channel} 비중 {dd_dir}"

    # 연령 관련
    if "연령" in dd_name:
        if dd_curr is not None:
            return f"평균 {dd_curr:.0f}세 ({dd_pct} {dd_dir})"

    # 회원 관련: 절대값 맥락
    if "회원" in dd_name:
        if "누적" in dd_name and dd_curr is not None:
            return f"누적 {dd_curr:.0f}명 ({dd_pct} {dd_dir})"
        if "신규" in dd_name:
            return f"신규 {dd_curr:.1f}명/일 ({dd_pct} {dd_dir})"
        if abs(dd_change) < 0.05:
            return f"{dd_curr:.1f}명 안정"
        return f"{dd_prev:.1f}→{dd_curr:.1f}명 {dd_dir}"

    # 근무 관련
    if "근무" in dd_name:
        if abs(dd_change) < 0.03:
            return f"{dd_curr:.1f}시간 유지"
        return f"{dd_prev:.1f}→{dd_curr:.1f}시간 {dd_dir}"

    # 장시간 대기 비율
    if "장시간" in dd_name:
        if dd_curr is not None and dd_curr == 0:
            return "장시간 대기 없음"
        if dd_curr is not None:
            return f"30분+ 비율 {dd_curr*100:.0f}%"

    # 탑승객 수 (평일/주말 등)
    if "탑승객" in dd_name:
        if "평일" in dd_name or "주말" in dd_name:
            daytype = "평일" if "평일" in dd_name else "주말"
            if abs(dd_change) < 0.05:
                return f"{daytype} {dd_curr:.1f}명 안정"
            return f"{daytype} {dd_curr:.1f}명 ({dd_pct} {dd_dir})"

    # ── 일반 패턴 (위에 해당 안 될 때) ──
    if kpi_change is None or kpi_change == 0:
        return f"{dd_pct} {dd_dir}"

    same_dir = (kpi_change > 0) == (dd_change > 0)

    # 안정 (±5% 미만)
    if abs(dd_change) < 0.05:
        return "안정 유지, 영향 제한적"

    if dd_is_pct:
        if same_dir:
            if abs(dd_change) >= 0.3:
                return f"비중 큰 폭 {dd_dir}, 주요 요인"
            return f"비중 {dd_dir} ({dd_pct})"
        else:
            if abs(dd_change) >= 0.3:
                return f"비중 역방향 {dd_dir} ({dd_pct})"
            return f"소폭 역방향 {dd_dir}"

    if same_dir:
        ratio = abs(dd_change) / abs(kpi_change) if abs(kpi_change) > 0.01 else 1
        if ratio > 2.0:
            return f"{dd_pct} {dd_dir}, 핵심 요인"
        elif ratio > 1.2:
            return f"{dd_pct} {dd_dir}, 주요 요인"
        elif ratio > 0.5:
            return f"동반 {dd_dir} ({dd_pct})"
        else:
            return f"소폭 {dd_dir}, 영향 제한적"
    else:
        if abs(dd_change) >= 0.3:
            return f"역방향 {dd_dir} ({dd_pct})"
        elif abs(dd_change) >= 0.1:
            return f"{dd_dir} 전환 ({dd_pct})"
        else:
            return f"소폭 역방향 {dd_dir}"


def compute_change(curr_val, prev_val):
    """비교기간 대비 변동률 계산"""
    if prev_val is None or curr_val is None:
        return None
    if prev_val == 0:
        return None if curr_val == 0 else float("inf")
    return (curr_val - prev_val) / abs(prev_val)


def should_trigger(change, trigger_def):
    """변동 트리거 조건 충족 여부"""
    if change is None:
        return False
    threshold = trigger_def["threshold"]
    if trigger_def.get("bidirectional"):
        return abs(change) >= abs(threshold)
    if threshold < 0:
        return change <= threshold
    return change >= threshold


def _is_negative_change(change, kpi_name):
    """변동이 부정적인지 판단. True=부정(빨강), False=긍정(파랑)"""
    if change is None:
        return False
    # 감소가 부정적인 지표 (수요/성장: 줄면 나쁨, 성공률: 줄면 나쁨)
    if "성공률" in kpi_name:
        return change < 0
    # 증가가 부정적인 지표 (대기시간/우회비율/이동시간/경로이탈: 늘면 나쁨)
    if "대기시간" in kpi_name or "우회비율" in kpi_name or "이동시간" in kpi_name or "경로이탈" in kpi_name:
        return change > 0
    # 일반 지표 (호출/탑승객/회원/차량 등): 감소가 부정적
    return change < 0


def status_label(change, kpi_name=""):
    """변동률에 따른 상태 라벨 (증가/감소 + 색상으로 긍정/부정 구분)"""
    if change is None:
        return "-"
    if abs(change) < 0.10:
        return "유지"
    return "증가" if change > 0 else "감소"


def color_status(stat_text, is_negative=False):
    """상태 텍스트에 색상 적용 (부정=빨강, 긍정=파랑)"""
    clean = stat_text.replace("⚠ ", "").strip()
    if clean == "유지" or clean == "-":
        return stat_text
    if is_negative:
        return f"{RED}{stat_text}{RESET}"
    else:
        return f"{BLUE}{stat_text}{RESET}"


def strip_ansi(text):
    """ANSI escape 코드 제거"""
    return re.sub(r'\033\[[0-9;]*m', '', text)


def kr_len(text):
    """한글 포함 문자열의 디스플레이 폭 계산 (한글/특수문자=2칸)"""
    text = strip_ansi(text)
    display_len = 0
    for ch in text:
        if '\uac00' <= ch <= '\ud7a3' or '\u4e00' <= ch <= '\u9fff':
            display_len += 2
        elif ch in '①②③④⑤⑥⑦⑧⑨⑩▲▼⚠━─═':
            display_len += 2
        else:
            display_len += 1
    return display_len


def pad_kr(text, width):
    """한글 포함 문자열을 고정 폭으로 패딩"""
    padding = max(0, width - kr_len(text))
    return text + " " * padding


def table_row(cells, widths):
    """| col1 | col2 | ... | 형태의 테이블 행 생성"""
    parts = []
    for cell, w in zip(cells, widths):
        parts.append(f" {pad_kr(cell, w)} ")
    return "|" + "|".join(parts) + "|"


def table_sep(widths, ch="─"):
    """테이블 구분선 생성"""
    parts = [ch * (w + 2) for w in widths]
    return "+" + "+".join(parts) + "+"


def generate_report(data_dir, compare_dir=None):
    """모니터링 보고서 생성 (고정 양식)"""
    region, period_fmt = parse_dir_info(data_dir)
    charts = load_charts(data_dir)
    prev_charts = load_charts(compare_dir) if compare_dir else None

    has_compare = prev_charts is not None and len(prev_charts) > 0
    prev_region, prev_period = parse_dir_info(compare_dir) if compare_dir else ("", "")

    # ── 운행일수 산출 ──
    op_days = get_operating_days(charts)
    prev_op_days = get_operating_days(prev_charts) if has_compare else None

    # ── 헤더 ──
    print(f"\n{'='*80}")
    print(f"  셔클 DRT 모니터링 보고서")
    print(f"  지역: {region} | 분석기간: {period_fmt}")
    if has_compare:
        print(f"  비교기간: {prev_period}")
    print(f"  생성: {datetime.now().strftime('%Y-%m-%d %H:%M')} | 차트: {len(charts)}개 로드")
    if op_days:
        op_info = f"운행일: {op_days}일"
        if prev_op_days:
            op_info += f" (비교: {prev_op_days}일)"
        print(f"  {op_info}")
    print(f"  지표 분석 체계: 수요/품질/공급/성장별 1차 핵심지표와 이를 세분화한 2차 세부지표")
    print(f"{'='*80}")

    # ── KPI 값 수집 ──
    all_kpi_results = []
    for cat in FRAMEWORK:
        for kpi in cat["primary"]:
            value, daily, chart = get_kpi_value(charts, kpi, op_days)
            is_pct = kpi.get("is_pct", False)
            prev_value = None
            prev_daily = OrderedDict()
            if has_compare:
                prev_value, prev_daily, _ = get_kpi_value(prev_charts, kpi, prev_op_days)
            change = compute_change(value, prev_value)
            all_kpi_results.append({
                "cat": cat, "kpi": kpi,
                "value": value, "daily": daily, "chart": chart,
                "prev_value": prev_value, "prev_daily": prev_daily,
                "change": change,
            })

    # ================================================================
    # 1. 1차 핵심지표 테이블
    # ================================================================
    print(f"\n  1. 1차 핵심지표 요약")

    # 컬럼 폭 정의
    W1 = [6, 20, 10, 10, 10, 8, 8]  # 구분, 지표, 비교기간, 분석기간, 변동값, 변동률, 상태
    headers1 = ["구분", "지표", "비교기간", "분석기간", "변동값", "변동률", "상태"]

    print(table_sep(W1))
    print(table_row(headers1, W1))
    print(table_sep(W1, "═"))

    triggered_list = []  # 10% 이상 변동된 KPI 모아두기
    stable_list = []     # 10% 이내 변동된 KPI (드릴다운 있는 것만)
    prev_cat_id = None

    for r in all_kpi_results:
        cat = r["cat"]
        kpi = r["kpi"]
        value = r["value"]
        prev_value = r["prev_value"]
        change = r["change"]
        is_pct = kpi.get("is_pct", False)
        unit = kpi["unit"]

        # 카테고리 구분선
        if cat["id"] != prev_cat_id and prev_cat_id is not None:
            print(table_sep(W1))
        prev_cat_id = cat["id"]

        # 값 포매팅
        curr_str = fmt_val(value, unit, is_pct) if value is not None else "-"
        prev_str = fmt_val(prev_value, unit, is_pct) if prev_value is not None else "-"

        # 변동값
        if value is not None and prev_value is not None:
            diff = value - prev_value
            if is_pct:
                diff_str = f"{diff*100:+.1f}%p"
            else:
                diff_str = f"{diff:+.1f}{unit}" if isinstance(diff, float) else f"{diff:+,}{unit}"
        else:
            diff_str = "-"

        # 변동률
        rate_str = f"{change*100:+.1f}%" if change is not None else "-"

        # 상태
        stat = status_label(change, kpi["name"])

        # 트리거 체크
        is_triggered = False
        if has_compare and change is not None:
            for trigger in kpi.get("triggers", []):
                if should_trigger(change, trigger):
                    is_triggered = True
                    triggered_list.append(r)
                    break
            # 트리거 안 됐지만 드릴다운이 있는 KPI → 안정 목록
            if not is_triggered:
                all_dds = []
                for trigger in kpi.get("triggers", []):
                    all_dds.extend(trigger.get("drilldowns", []))
                if all_dds:
                    stable_list.append(r)

        # ±10% 이상이면 색상, ±20% 이상이면 ⚠ 추가
        if change is not None and abs(change) >= 0.20:
            stat_display = f"⚠ {stat}"
        else:
            stat_display = stat
        is_neg = _is_negative_change(change, kpi["name"])
        stat_display = color_status(stat_display, is_neg)

        print(table_row([cat["category"], kpi["name"], prev_str, curr_str, diff_str, rate_str, stat_display], W1))

    print(table_sep(W1))

    if not has_compare:
        print(f"\n  * 비교기간 비교 데이터 없음 — 비교기간 데이터 수집 후 비교 분석이 활성화됩니다.")

    # ================================================================
    # 2. 2차 세부지표_변동 (±10% 이상 변동)
    # ================================================================
    if triggered_list:
        print(f"\n  2. 2차 세부지표_변동 (1차 핵심지표 ±10% 이상 변동)")

        # 컬럼 폭: 구분, 지표, 비교기간, 분석기간, 변동값, 변동률, 포인트
        W2 = [20, 20, 12, 12, 10, 8, 34]
        headers2 = ["구분", "세부지표", "비교기간", "분석기간", "변동값", "변동률", "포인트"]

        for r in triggered_list:
            kpi = r["kpi"]
            change = r["change"]

            direction_icon = "▲" if change > 0 else "▼"
            print(f"\n  {direction_icon} {kpi['name']} ({change*100:+.1f}%)")

            # 해당 KPI의 트리거된 드릴다운 찾기
            active_dds = []
            for trigger in kpi.get("triggers", []):
                if should_trigger(change, trigger):
                    active_dds.extend(trigger.get("drilldowns", []))

            if not active_dds:
                continue

            print(table_sep(W2))
            print(table_row(headers2, W2))
            print(table_sep(W2, "═"))

            # 중복 제거
            seen = set()
            for dd in active_dds:
                if dd["name"] in seen:
                    continue
                seen.add(dd["name"])

                # 분석기간 값 (숫자 + 표시용)
                dd_is_pct = dd.get("is_pct", False)
                dd_stats, _ = get_drilldown_value(charts, dd, op_days)
                curr_num = None
                if dd_stats and dd_stats["count"] > 0:
                    curr_num = dd_stats["avg"]
                    curr_dd_str = fmt_val(dd_stats["avg"], is_pct=dd_is_pct)
                else:
                    curr_dd_str = "-"

                # 동적 이름 처리 (피크/비피크 시간대 등)
                dd_display_name = _resolve_dynamic_name(dd, dd_stats)

                # 비교기간 값
                prev_num = None
                if has_compare and prev_charts:
                    dd_prev_stats, _ = get_drilldown_value(prev_charts, dd, prev_op_days)
                    if dd_prev_stats and dd_prev_stats["count"] > 0:
                        prev_num = dd_prev_stats["avg"]
                        prev_dd_str = fmt_val(dd_prev_stats["avg"], is_pct=dd_is_pct)
                    else:
                        prev_dd_str = "-"
                else:
                    prev_dd_str = "-"

                # 사용 후 임시 시간대 정보 정리
                for k in list(dd.keys()):
                    if k.startswith("_"):
                        del dd[k]

                # 변동값, 변동률 계산
                if curr_num is not None and prev_num is not None:
                    dd_diff = curr_num - prev_num
                    if dd_is_pct:
                        dd_diff_str = f"{dd_diff*100:+.1f}%p"
                    else:
                        dd_diff_str = f"{dd_diff:+.1f}"
                    dd_change = compute_change(curr_num, prev_num)
                    dd_rate_str = f"{dd_change*100:+.1f}%" if dd_change is not None else "-"
                else:
                    dd_diff_str = "-"
                    dd_rate_str = "-"

                dd_change_val = compute_change(curr_num, prev_num) if curr_num is not None and prev_num is not None else None
                point = dynamic_point(kpi["name"], change, dd_display_name, dd_change_val, curr_num, prev_num, dd_is_pct)
                print(table_row([kpi["name"], dd_display_name, prev_dd_str, curr_dd_str, dd_diff_str, dd_rate_str, point], W2))

            print(table_sep(W2))

    # ================================================================
    # 3. 핵심 해석
    # ================================================================
    print(f"\n  3. 핵심 해석")

    # KPI 맵 + 일별 데이터 기반 자유 해석 생성
    kpi_map = {}
    for r in all_kpi_results:
        kpi_map[r["kpi"]["name"]] = r

    calls_r = kpi_map.get("실시간 호출 건수(일평균)", {})
    completed_r = kpi_map.get("이동완료 호출 건수(일평균)", {})
    passengers_r = kpi_map.get("총 탑승객 수(일평균)", {})
    wait_r = kpi_map.get("평균 대기시간", {})
    detour_r = kpi_map.get("평균 우회비율", {})
    travel_r = kpi_map.get("평균 이동시간", {})
    vehicles_r = kpi_map.get("운행차량 대수", {})
    success_r = kpi_map.get("가호출 성공률", {})
    dau_r = kpi_map.get("DAU (일간 활성 회원)", {})
    newmem_r = kpi_map.get("신규 지역 회원(일평균)", {})

    insights = []

    # ─ 수요 종합 ─
    calls_v = calls_r.get("value")
    completed_v = completed_r.get("value")
    passengers_v = passengers_r.get("value")
    calls_ch = calls_r.get("change")
    completed_ch = completed_r.get("change")
    passengers_ch = passengers_r.get("change")
    calls_daily = calls_r.get("daily", OrderedDict())
    completed_daily = completed_r.get("daily", OrderedDict())

    if calls_v and (calls_ch is not None and abs(calls_ch) >= 0.10):
        # 운행일 수 파악 (호출 있는 날)
        active_days = [d for d, v in calls_daily.items() if v > 0]
        prev_daily = calls_r.get("prev_daily", OrderedDict())
        prev_active_days = [d for d, v in prev_daily.items() if v > 0]
        day_info = ""
        if active_days:
            day_list = ",".join(active_days)
            day_info = f" — 분석기간 운행 {len(active_days)}일({day_list})"
            if prev_active_days:
                day_info += f", 비교기간 {len(prev_active_days)}일"
                if len(active_days) < len(prev_active_days):
                    day_info += f"로 운행일수 차이 가능성"

        parts = []
        if calls_ch is not None:
            parts.append(f"호출({calls_ch*100:+.0f}%)")
        if completed_ch is not None and abs(completed_ch) >= 0.10:
            parts.append(f"이동완료({completed_ch*100:+.0f}%)")
        if passengers_ch is not None and abs(passengers_ch) >= 0.10:
            parts.append(f"탑승객({passengers_ch*100:+.0f}%)")

        direction = "감소" if calls_ch < 0 else "증가"
        insight = f"수요 {direction}: {', '.join(parts)} 모두 {'하락' if calls_ch < 0 else '상승'}{day_info}"
        insights.append(insight)

    # ─ 이동완료율 ─
    if calls_v and completed_v:
        rate = completed_v / calls_v * 100
        prev_calls = calls_r.get("prev_value")
        prev_completed = completed_r.get("prev_value")

        # 호출취소/배차실패 수치
        cancel_stats, cancel_daily = get_drilldown_value(charts, {"name": "", "match": {"includes": ["일별", "실시간 호출 결과"]}, "col": "호출취소"}, op_days)
        fail_stats, fail_daily = get_drilldown_value(charts, {"name": "", "match": {"includes": ["일별", "실시간 호출 결과"]}, "col": "배차실패"}, op_days)
        cancel_total = cancel_stats["total"] if cancel_stats else 0
        fail_total = fail_stats["total"] if fail_stats else 0

        if prev_calls and prev_completed and prev_calls > 0:
            prev_rate = prev_completed / prev_calls * 100
            rate_diff = rate - prev_rate
            extra = ""
            if cancel_total or fail_total:
                extra = f" (호출취소 {cancel_total:.0f}건, 배차실패 {fail_total:.0f}건)"
            insights.append(
                f"이동완료율 {prev_rate:.1f}% → {rate:.1f}% ({rate_diff:+.1f}%p): "
                f"호출 {calls_v:.0f}건 중 {completed_v:.0f}건 완료{extra}"
            )

    # ─ 대기시간 ─
    wait_v = wait_r.get("value")
    wait_ch = wait_r.get("change")
    if wait_v is not None and wait_ch is not None and abs(wait_ch) >= 0.10:
        prev_wait = wait_r.get("prev_value")
        wait_daily = wait_r.get("daily", OrderedDict())
        # 최대 대기일 찾기
        max_day = max(wait_daily, key=wait_daily.get) if wait_daily else None
        max_val = wait_daily.get(max_day, 0) if max_day else 0
        # 상위10%
        top10_stats, top10_daily = get_drilldown_value(charts, {"name": "", "match": {"includes": ["상위10% 대기시간"]}}, op_days)
        top10_max = ""
        if top10_daily:
            t_max_day = max(top10_daily, key=top10_daily.get)
            top10_max = f" — 특히 {t_max_day} 상위10% 대기시간 {top10_daily[t_max_day]:.1f}분으로 극단치 발생"

        direction = "급등" if wait_ch > 0 else "개선"
        insights.append(
            f"대기시간 {direction}: {fmt_val(prev_wait, '분') if prev_wait else '?'} → {fmt_val(wait_v, '분')} "
            f"({wait_ch*100:+.0f}%){top10_max}"
        )

    # ─ 가호출 성공률 ─
    sr_v = success_r.get("value")
    sr_ch = success_r.get("change")
    if sr_v is not None and sr_ch is not None and abs(sr_ch) >= 0.10:
        prev_sr = success_r.get("prev_value")
        # 가호출 수
        vhcall_stats, _ = get_drilldown_value(charts, {"name": "", "match": {"includes": ["가호출 수"], "excludes": ["순 회원", "성공", "결과", "성공률", "일별"]}}, op_days)
        vhcall_str = f", 가호출 시도 일평균 {fmt_val(vhcall_stats['avg'])}건" if vhcall_stats and vhcall_stats["count"] > 0 else ""
        insights.append(
            f"가호출 성공률 {'급락' if sr_ch < 0 else '상승'}: "
            f"{fmt_val(prev_sr, '', True) if prev_sr is not None else '?'} → {fmt_val(sr_v, '', True)}"
            f"{vhcall_str}"
            f"{' — 가호출 시도는 있으나 실제 호출 전환율 매우 낮음' if sr_v < 0.05 else ''}"
        )

    # ─ 성장 ─
    dau_v = dau_r.get("value")
    dau_ch = dau_r.get("change")
    newmem_v = newmem_r.get("value")
    newmem_ch = newmem_r.get("change")
    # 누적 회원
    cumul_stats, cumul_daily = get_drilldown_value(charts, {"name": "", "match": {"includes": ["누적 지역 회원"], "excludes": ["일별"]}}, op_days)
    cumul_last = list(cumul_daily.values())[-1] if cumul_daily else None

    if (dau_ch is not None and abs(dau_ch) >= 0.10) or (newmem_ch is not None and abs(newmem_ch) >= 0.10):
        parts = []
        if dau_ch is not None and abs(dau_ch) >= 0.10:
            parts.append(f"DAU {dau_ch*100:+.0f}%")
        if newmem_ch is not None and abs(newmem_ch) >= 0.10:
            prev_nm = newmem_r.get("prev_value")
            nm_str = f"신규회원 {prev_nm:.0f}→{newmem_v:.0f}명({newmem_ch*100:+.0f}%)" if prev_nm is not None else f"신규회원 {newmem_v:.0f}명"
            parts.append(nm_str)
        cumul_str = f", 누적 {cumul_last:.0f}명" if cumul_last else ""
        # 수요와의 괴리 판단
        gap = ""
        if calls_ch is not None and calls_ch < -0.10 and newmem_ch is not None and newmem_ch > 0.10:
            gap = " — 관심은 늘고 있으나 실 이용으로 연결이 부족"
        elif newmem_ch is not None and newmem_ch > 0.10:
            gap = " — 이용자 기반 확대 추세"
        insights.append(f"성장 지표 긍정적: {', '.join(parts)}{cumul_str}{gap}")

    # ─ 이동시간/우회비율 ─
    travel_v = travel_r.get("value")
    travel_ch = travel_r.get("change")
    detour_v = detour_r.get("value")
    detour_ch = detour_r.get("change")
    if travel_ch is not None and abs(travel_ch) >= 0.10:
        prev_travel = travel_r.get("prev_value")
        detour_info = ""
        if detour_ch is not None:
            detour_info = f" — 우회비율{'도 소폭 개선' if detour_ch < 0 else ' 유지' if abs(detour_ch) < 0.05 else '은 소폭 증가'}, 경로 효율성 {'향상' if travel_ch < 0 else '저하'}"
        direction = "개선" if travel_ch < 0 else "증가"
        insights.append(
            f"이동시간 {direction}: {fmt_val(prev_travel, '분') if prev_travel else '?'} → {fmt_val(travel_v, '분')} "
            f"({travel_ch*100:+.0f}%){detour_info}"
        )

    # ─ 공급 효율 (호당 탑승객 등 파생 지표) ─
    vehicles_v = vehicles_r.get("value")
    if passengers_v and completed_v and completed_v > 0:
        per_call = passengers_v / completed_v
        if per_call > 1.2 or per_call < 0.8:
            insights.append(f"호출당 평균 탑승객 {per_call:.1f}명: {'동승 이용 활발' if per_call > 1.3 else '1인 이용 위주'}")

    # ─ 최소 4개 보장: 변동 조건 미충족 시 현황 요약으로 보충 ─
    if len(insights) < 4:
        # 수요 현황 (변동 조건 미충족 시)
        if not any("수요" in ins for ins in insights) and calls_v is not None:
            calls_ch_str = f"({calls_ch*100:+.1f}%)" if calls_ch is not None else ""
            insights.append(f"수요 현황: 일평균 호출 {fmt_val(calls_v, '건')}{calls_ch_str}, 이동완료 {fmt_val(completed_v, '건')}, 탑승객 {fmt_val(passengers_v, '명')}")

    if len(insights) < 4:
        # 대기시간 현황 (변동 조건 미충족 시)
        if not any("대기시간" in ins for ins in insights) and wait_v is not None:
            prev_wait = wait_r.get("prev_value")
            if prev_wait is not None:
                insights.append(f"대기시간 안정: {fmt_val(prev_wait, '분')} → {fmt_val(wait_v, '분')} ({wait_ch*100:+.1f}%) — 큰 변동 없음")
            else:
                insights.append(f"대기시간 현황: 평균 {fmt_val(wait_v, '분')}")

    if len(insights) < 4:
        # 가호출 성공률 현황 (변동 조건 미충족 시)
        if not any("가호출 성공률" in ins for ins in insights) and sr_v is not None:
            prev_sr = success_r.get("prev_value")
            if prev_sr is not None:
                insights.append(f"가호출 성공률 안정: {fmt_val(prev_sr, '', True)} → {fmt_val(sr_v, '', True)} ({sr_ch*100:+.1f}%)")
            else:
                insights.append(f"가호출 성공률 현황: {fmt_val(sr_v, '', True)}")

    if len(insights) < 4:
        # 성장 현황 (변동 조건 미충족 시)
        if not any("성장" in ins for ins in insights) and dau_v is not None:
            cumul_str = f", 누적회원 {cumul_last:.0f}명" if cumul_last else ""
            insights.append(f"성장 현황: DAU {fmt_val(dau_v, '명')}, 신규회원 일평균 {fmt_val(newmem_v, '명')}{cumul_str}")

    if len(insights) < 4:
        # 공급 현황
        if vehicles_v is not None:
            optime_r = kpi_map.get("대당 운행시간(일평균)", {})
            optime_v = optime_r.get("value")
            insights.append(f"공급 현황: 운행차량 {fmt_val(vehicles_v, '대')}, 대당 운행시간 {fmt_val(optime_v, '시간') if optime_v else '-'}")

    # 출력
    if insights:
        for ins in insights:
            print(f"  * {ins}")
    else:
        print(f"  * 주요 변동 사항 없음.")

    # 분석 참고
    print(f"\n  ※ 변동 감지 기준: 비교기간 대비 ±10% | 분석 흐름: 수요→품질→공급→성장")
    if not has_compare:
        print(f"  ※ 비교 데이터 없음: python monitoring_report.py <분석기간> <비교기간>")

    # ================================================================
    # 4. [부록] 2차 세부지표_안정 (10% 이내 변동)
    # ================================================================
    if stable_list:
        print(f"\n  4. [부록] 2차 세부지표_안정 (10% 이내 변동)")

        W3 = [20, 20, 12, 12, 10, 8, 34]
        headers3 = ["구분", "세부지표", "비교기간", "분석기간", "변동값", "변동률", "포인트"]

        for r in stable_list:
            kpi = r["kpi"]
            change = r["change"]

            direction_icon = "▲" if change > 0 else ("▼" if change < 0 else "─")
            print(f"\n  {direction_icon} {kpi['name']} ({change*100:+.1f}%)")

            # 해당 KPI의 모든 드릴다운 수집
            all_dds = []
            for trigger in kpi.get("triggers", []):
                all_dds.extend(trigger.get("drilldowns", []))

            if not all_dds:
                continue

            print(table_sep(W3))
            print(table_row(headers3, W3))
            print(table_sep(W3, "═"))

            # 중복 제거
            seen = set()
            for dd in all_dds:
                if dd["name"] in seen:
                    continue
                seen.add(dd["name"])

                # 분석기간 값
                dd_is_pct = dd.get("is_pct", False)
                dd_stats, _ = get_drilldown_value(charts, dd, op_days)
                curr_num = None
                if dd_stats and dd_stats["count"] > 0:
                    curr_num = dd_stats["avg"]
                    curr_dd_str = fmt_val(dd_stats["avg"], is_pct=dd_is_pct)
                else:
                    curr_dd_str = "-"

                # 동적 이름 처리
                dd_display_name = _resolve_dynamic_name(dd, dd_stats)

                # 비교기간 값
                prev_num = None
                if has_compare and prev_charts:
                    dd_prev_stats, _ = get_drilldown_value(prev_charts, dd, prev_op_days)
                    if dd_prev_stats and dd_prev_stats["count"] > 0:
                        prev_num = dd_prev_stats["avg"]
                        prev_dd_str = fmt_val(dd_prev_stats["avg"], is_pct=dd_is_pct)
                    else:
                        prev_dd_str = "-"
                else:
                    prev_dd_str = "-"

                # 사용 후 임시 시간대 정보 정리
                for k in list(dd.keys()):
                    if k.startswith("_"):
                        del dd[k]

                # 변동값, 변동률 계산
                if curr_num is not None and prev_num is not None:
                    dd_diff = curr_num - prev_num
                    if dd_is_pct:
                        dd_diff_str = f"{dd_diff*100:+.1f}%p"
                    else:
                        dd_diff_str = f"{dd_diff:+.1f}"
                    dd_change = compute_change(curr_num, prev_num)
                    dd_rate_str = f"{dd_change*100:+.1f}%" if dd_change is not None else "-"
                else:
                    dd_diff_str = "-"
                    dd_rate_str = "-"

                dd_change_val = compute_change(curr_num, prev_num) if curr_num is not None and prev_num is not None else None
                point = dynamic_point(kpi["name"], change, dd_display_name, dd_change_val, curr_num, prev_num, dd_is_pct)
                print(table_row([kpi["name"], dd_display_name, prev_dd_str, curr_dd_str, dd_diff_str, dd_rate_str, point], W3))

            print(table_sep(W3))

    print(f"{'='*80}\n")


# ================================================================
# 메인
# ================================================================

def auto_export(data_dir, compare_dir=None):
    """리포트 생성 후 HTML/XLSX 파일 자동 저장"""
    try:
        from export_report import build_report_data, export_html, export_xlsx
    except ImportError as e:
        print(f"\n  [내보내기 건너뜀] 필요 패키지 미설치: {e}")
        return

    data = build_report_data(data_dir, compare_dir)

    # 저장 경로: shucle_report/{지역}/{기간}/
    base_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shucle_report")
    save_dir = os.path.join(base_dir, data["region"], data["period_code"])
    os.makedirs(save_dir, exist_ok=True)

    prefix = f"report_{data['region']}_{data['period_code']}"

    export_html(data, os.path.join(save_dir, f"{prefix}.html"))
    export_xlsx(data, os.path.join(save_dir, f"{prefix}.xlsx"))

    print(f"\n  [파일 저장 완료] {save_dir}")
    print(f"    - {prefix}.html")
    print(f"    - {prefix}.xlsx")

    # GitHub Pages 자동 배포
    auto_push(save_dir, data["region"], data["period_code"])


def auto_push(save_dir, region, period_code):
    """리포트 파일을 GitHub에 자동 push하여 Pages 배포"""
    import subprocess

    repo_root = os.path.dirname(os.path.abspath(__file__))

    # git repo 확인
    result = subprocess.run(
        ["git", "remote", "-v"], cwd=repo_root,
        capture_output=True, text=True, encoding="utf-8"
    )
    if "origin" not in result.stdout:
        print("\n  [push 건너뜀] git remote 'origin' 미설정")
        return

    # 리포트 파일 staging
    rel_path = os.path.relpath(save_dir, repo_root)
    subprocess.run(
        ["git", "add", rel_path], cwd=repo_root,
        capture_output=True, text=True, encoding="utf-8"
    )

    # 변경사항 확인
    diff = subprocess.run(
        ["git", "diff", "--cached", "--name-only"], cwd=repo_root,
        capture_output=True, text=True, encoding="utf-8"
    )
    if not diff.stdout.strip():
        print("\n  [push 건너뜀] 변경된 리포트 파일 없음")
        return

    # commit + push
    msg = f"Update report: {region} {period_code}"
    subprocess.run(
        ["git", "commit", "-m", msg], cwd=repo_root,
        capture_output=True, text=True, encoding="utf-8"
    )
    push_result = subprocess.run(
        ["git", "push", "origin"], cwd=repo_root,
        capture_output=True, text=True, encoding="utf-8"
    )

    if push_result.returncode == 0:
        pages_url = f"https://hangil-kim.github.io/shucle-monitor/shucle_report/{region}/{period_code}/report_{region}_{period_code}.html"
        print(f"\n  [GitHub push 완료] 1~2분 후 웹 링크에서 확인 가능:")
        print(f"    {pages_url}")
    else:
        print(f"\n  [push 실패] {push_result.stderr.strip()}")


def main():
    if len(sys.argv) < 2:
        print("사용법: python monitoring_report.py <데이터_디렉토리> [비교_디렉토리]")
        print("예시:   python monitoring_report.py shucle_data/영덕관광/20260219_20260225")
        data_dir = input("\n데이터 디렉토리: ").strip()
        if not data_dir:
            print("디렉토리를 입력해주세요.")
            return
    else:
        data_dir = sys.argv[1]

    compare_dir = sys.argv[2] if len(sys.argv) >= 3 else None

    if not os.path.isdir(data_dir):
        print(f"[오류] 디렉토리가 존재하지 않습니다: {data_dir}")
        return
    if compare_dir and not os.path.isdir(compare_dir):
        print(f"[오류] 비교 디렉토리가 존재하지 않습니다: {compare_dir}")
        return

    generate_report(data_dir, compare_dir)
    auto_export(data_dir, compare_dir)


if __name__ == "__main__":
    main()
