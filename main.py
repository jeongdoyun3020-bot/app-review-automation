"""
알송(Alsong) 앱 리뷰 자동 수집 및 이메일 발송 스크립트
- Google Play: com.estsoft.alsong
- App Store ID: 364013007
- 매주 일요일 오후 5시(KST) 실행 (GitHub Actions cron)
"""

import os
import re
import json
import smtplib
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from google_play_scraper import reviews, Sort
from app_store_scraper import AppStore
import anthropic

# ──────────────────────────────────────────────
# 설정값 (GitHub Actions Secrets에서 주입)
# ──────────────────────────────────────────────
GOOGLE_PLAY_APP_ID = "com.estsoft.alsong"
APP_STORE_APP_ID   = "364013007"
APP_STORE_APP_NAME = "alsong"

RECIPIENT_EMAIL  = "jdy@estsoft.com"
SENDER_EMAIL     = os.environ["SENDER_EMAIL"]       # Gmail 주소
EMAIL_PASSWORD   = os.environ["EMAIL_PASSWORD"]     # Gmail 앱 비밀번호
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"] # Claude API 키


# ──────────────────────────────────────────────
# 1. 리뷰 수집 함수
# ──────────────────────────────────────────────

def get_cutoff_date() -> datetime:
    """7일 전 기준 날짜(UTC) 반환"""
    return datetime.now(timezone.utc) - timedelta(days=7)


def fetch_google_play_reviews() -> list[dict]:
    """Google Play에서 최근 7일치 리뷰 수집"""
    cutoff = get_cutoff_date()
    collected = []
    continuation_token = None

    while True:
        batch, continuation_token = reviews(
            GOOGLE_PLAY_APP_ID,
            lang="ko",
            country="kr",
            sort=Sort.NEWEST,
            count=200,
            continuation_token=continuation_token,
        )

        stop = False
        for r in batch:
            review_date = r["at"]
            if review_date.tzinfo is None:
                review_date = review_date.replace(tzinfo=timezone.utc)

            if review_date >= cutoff:
                collected.append({
                    "source":  "Google Play",
                    "author":  r.get("userName", "익명"),
                    "rating":  r.get("score", 0),
                    "date":    review_date.strftime("%Y-%m-%d"),
                    "content": r.get("content", ""),
                })
            else:
                stop = True  # 날짜 순 정렬이므로 이후는 불필요

        if stop or not continuation_token:
            break

    return collected


def fetch_app_store_reviews() -> list[dict]:
    """App Store에서 최근 7일치 리뷰 수집"""
    cutoff = get_cutoff_date()
    app = AppStore(country="kr", app_name=APP_STORE_APP_NAME, app_id=APP_STORE_APP_ID)
    app.review(how_many=200)

    collected = []
    for r in app.reviews:
        review_date = r.get("date")
        if review_date is None:
            continue
        if review_date.tzinfo is None:
            review_date = review_date.replace(tzinfo=timezone.utc)

        if review_date >= cutoff:
            collected.append({
                "source":  "App Store",
                "author":  r.get("userName", "익명"),
                "rating":  r.get("rating", 0),
                "date":    review_date.strftime("%Y-%m-%d"),
                "content": r.get("review", ""),
            })

    return collected


# ──────────────────────────────────────────────
# 2. AI 요약 함수 (Claude API)
# ──────────────────────────────────────────────

def summarize_with_claude(reviews_list: list[dict]) -> dict:
    """수집된 리뷰를 Claude로 분석해 3가지 카테고리로 요약"""
    if not reviews_list:
        empty = "이번 주 수집된 리뷰가 없습니다."
        return {"bugs": empty, "suggestions": empty, "praise": empty}

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # 리뷰 텍스트 구성 (최대 150건, 토큰 절약)
    sample = reviews_list[:150]
    reviews_text = "\n\n".join(
        f"[{r['source']}] 별점 {r['rating']}/5 | {r['date']}\n{r['content']}"
        for r in sample
    )

    prompt = f"""아래는 알송(Alsong) 앱의 최근 7일간 구글 플레이 및 앱스토어 리뷰입니다.
분석하여 반드시 아래 JSON 형식으로만 응답하세요. JSON 외 다른 텍스트는 출력하지 마세요.

리뷰 목록:
{reviews_text}

응답 형식 (각 항목은 bullet(•) 기호로 3~5개 줄로 작성):
{{
    "bugs": "• 버그1 설명\\n• 버그2 설명\\n...",
    "suggestions": "• 건의사항1\\n• 건의사항2\\n...",
    "praise": "• 칭찬키워드1 - 구체 내용\\n• 칭찬키워드2 - 구체 내용\\n..."
}}"""

    message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=1200,
        messages=[{"role": "user", "content": prompt}],
    )

    response_text = message.content[0].text.strip()

    # JSON 파싱
    json_match = re.search(r"\{[\s\S]*\}", response_text)
    if json_match:
        try:
            return json.loads(json_match.group())
        except json.JSONDecodeError:
            pass

    # 파싱 실패 시 원문 반환
    return {"bugs": response_text, "suggestions": "–", "praise": "–"}


# ──────────────────────────────────────────────
# 3. HTML 리포트 생성
# ──────────────────────────────────────────────

def star_icons(rating: int) -> str:
    return "★" * rating + "☆" * (5 - rating)


def create_html_report(
    all_reviews: list[dict],
    summary: dict,
    start_date: str,
    end_date: str,
) -> str:
    gp = [r for r in all_reviews if r["source"] == "Google Play"]
    ap = [r for r in all_reviews if r["source"] == "App Store"]

    gp_avg = round(sum(r["rating"] for r in gp) / len(gp), 1) if gp else 0
    ap_avg = round(sum(r["rating"] for r in ap) / len(ap), 1) if ap else 0

    def make_rows(reviews_slice: list[dict]) -> str:
        rows = ""
        for r in reviews_slice:
            src_color = "#34A853" if r["source"] == "Google Play" else "#007AFF"
            preview = r["content"][:220] + ("…" if len(r["content"]) > 220 else "")
            rows += f"""
            <tr>
              <td><span class="badge" style="background:{src_color}">{r["source"]}</span></td>
              <td class="center" style="color:#f4a100;letter-spacing:1px">{star_icons(r["rating"])}</td>
              <td class="center">{r["date"]}</td>
              <td class="center">{r["author"]}</td>
              <td>{preview}</td>
            </tr>"""
        return rows

    review_rows = make_rows(all_reviews[:50])  # 최대 50건 표시

    bugs_html        = summary.get("bugs", "–").replace("\n", "<br>")
    suggestions_html = summary.get("suggestions", "–").replace("\n", "<br>")
    praise_html      = summary.get("praise", "–").replace("\n", "<br>")

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>알송 주간 리뷰 리포트</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: 'Malgun Gothic', 'Apple SD Gothic Neo', Arial, sans-serif;
    background: #f0f2f5;
    color: #333;
    padding: 24px;
  }}
  .wrap {{ max-width: 920px; margin: 0 auto; }}

  /* Header */
  .header {{
    background: linear-gradient(135deg, #4f46e5 0%, #7c3aed 100%);
    color: #fff;
    padding: 32px 28px;
    border-radius: 14px;
    margin-bottom: 20px;
  }}
  .header h1 {{ font-size: 24px; margin-bottom: 6px; }}
  .header p  {{ font-size: 14px; opacity: .85; }}

  /* Stat cards */
  .stats {{ display: flex; gap: 14px; margin-bottom: 20px; flex-wrap: wrap; }}
  .stat-card {{
    flex: 1; min-width: 150px;
    background: #fff;
    border-radius: 12px;
    padding: 18px;
    text-align: center;
    box-shadow: 0 1px 6px rgba(0,0,0,.08);
  }}
  .stat-num  {{ font-size: 32px; font-weight: 700; }}
  .stat-label{{ font-size: 13px; color: #777; margin-top: 4px; }}
  .stat-sub  {{ font-size: 12px; color: #aaa; margin-top: 2px; }}

  /* Card */
  .card {{
    background: #fff;
    border-radius: 12px;
    padding: 22px 24px;
    margin-bottom: 20px;
    box-shadow: 0 1px 6px rgba(0,0,0,.08);
  }}
  .section-title {{
    font-size: 16px; font-weight: 700;
    border-left: 4px solid #4f46e5;
    padding-left: 10px;
    margin-bottom: 16px;
  }}

  /* AI summary boxes */
  .ai-box {{
    border-radius: 8px;
    padding: 14px 16px;
    margin-bottom: 12px;
    font-size: 14px;
    line-height: 1.7;
  }}
  .ai-box h4 {{ font-size: 14px; margin-bottom: 8px; }}
  .bug-box  {{ background:#fff5f5; border-left:4px solid #e53e3e; }}
  .sug-box  {{ background:#fffbf0; border-left:4px solid #d97706; }}
  .prs-box  {{ background:#f0fff4; border-left:4px solid #38a169; }}

  /* Table */
  table {{ width:100%; border-collapse:collapse; font-size:13px; }}
  th {{
    background:#4f46e5; color:#fff;
    padding: 10px 12px; text-align:center;
  }}
  td {{ padding: 9px 12px; border-bottom: 1px solid #eee; vertical-align:top; }}
  tr:hover td {{ background:#fafafa; }}
  .center {{ text-align:center; }}
  .badge {{
    display:inline-block; color:#fff;
    padding: 2px 8px; border-radius:20px;
    font-size:11px; white-space:nowrap;
  }}

  /* Footer */
  .footer {{ text-align:center; font-size:12px; color:#aaa; padding: 16px 0; }}
</style>
</head>
<body>
<div class="wrap">

  <!-- Header -->
  <div class="header">
    <h1>🎵 알송 (Alsong) 주간 앱 리뷰 리포트</h1>
    <p>수집 기간: {start_date} ~ {end_date} &nbsp;|&nbsp; 자동 생성: {generated_at}</p>
    <div style="margin-top:16px;display:flex;gap:10px;flex-wrap:wrap;">
      <a href="https://play.google.com/store/apps/details?id=com.estsoft.alsong"
         style="display:inline-flex;align-items:center;gap:6px;background:rgba(255,255,255,0.18);color:#fff;text-decoration:none;padding:8px 16px;border-radius:20px;font-size:13px;font-weight:600;border:1px solid rgba(255,255,255,0.35);">
        ▶ Google Play 스토어
      </a>
      <a href="https://apps.apple.com/kr/app/alsong/id364013007"
         style="display:inline-flex;align-items:center;gap:6px;background:rgba(255,255,255,0.18);color:#fff;text-decoration:none;padding:8px 16px;border-radius:20px;font-size:13px;font-weight:600;border:1px solid rgba(255,255,255,0.35);">
         App Store
      </a>
    </div>
  </div>

  <!-- 통계 카드 -->
  <div class="stats">
    <div class="stat-card">
      <div class="stat-num" style="color:#4f46e5">{len(all_reviews)}</div>
      <div class="stat-label">총 리뷰 수</div>
    </div>
    <div class="stat-card">
      <div class="stat-num" style="color:#34A853">{len(gp)}</div>
      <div class="stat-label">Google Play</div>
      <div class="stat-sub">평균 ★ {gp_avg}</div>
    </div>
    <div class="stat-card">
      <div class="stat-num" style="color:#007AFF">{len(ap)}</div>
      <div class="stat-label">App Store</div>
      <div class="stat-sub">평균 ★ {ap_avg}</div>
    </div>
    <div class="stat-card">
      <div class="stat-num" style="color:#e53e3e">{sum(1 for r in all_reviews if r["rating"] <= 2)}</div>
      <div class="stat-label">부정 리뷰 (★1~2)</div>
    </div>
    <div class="stat-card">
      <div class="stat-num" style="color:#38a169">{sum(1 for r in all_reviews if r["rating"] >= 4)}</div>
      <div class="stat-label">긍정 리뷰 (★4~5)</div>
    </div>
  </div>

  <!-- AI 요약 -->
  <div class="card">
    <div class="section-title">🤖 AI 분석 요약 (Claude)</div>

    <div class="ai-box bug-box">
      <h4>🐛 주요 버그 제보</h4>
      <div>{bugs_html}</div>
    </div>

    <div class="ai-box sug-box">
      <h4>💡 사용자 건의 사항</h4>
      <div>{suggestions_html}</div>
    </div>

    <div class="ai-box prs-box">
      <h4>👍 칭찬 키워드</h4>
      <div>{praise_html}</div>
    </div>
  </div>

  <!-- 리뷰 테이블 -->
  <div class="card">
    <div class="section-title">📋 리뷰 목록 (최신 50건)</div>
    <table>
      <thead>
        <tr>
          <th style="width:110px">플랫폼</th>
          <th style="width:90px">별점</th>
          <th style="width:90px">날짜</th>
          <th style="width:90px">작성자</th>
          <th>내용</th>
        </tr>
      </thead>
      <tbody>
        {review_rows if review_rows else '<tr><td colspan="5" class="center" style="padding:20px;color:#aaa">이번 주 수집된 리뷰가 없습니다.</td></tr>'}
      </tbody>
    </table>
  </div>

  <div class="footer">
    이 리포트는 GitHub Actions + Claude AI를 통해 자동으로 생성되었습니다.
  </div>

</div>
</body>
</html>"""


# ──────────────────────────────────────────────
# 4. 이메일 발송
# ──────────────────────────────────────────────

def send_email(html_content: str, subject: str) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = SENDER_EMAIL
    msg["To"]      = RECIPIENT_EMAIL
    msg.attach(MIMEText(html_content, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(SENDER_EMAIL, EMAIL_PASSWORD)
        server.sendmail(SENDER_EMAIL, RECIPIENT_EMAIL, msg.as_string())
        print(f"  ✅ 이메일 발송 완료 → {RECIPIENT_EMAIL}")


# ──────────────────────────────────────────────
# 5. 메인 실행
# ──────────────────────────────────────────────

def main():
    now     = datetime.now(timezone.utc)
    cutoff  = now - timedelta(days=7)
    start_s = cutoff.strftime("%Y-%m-%d")
    end_s   = now.strftime("%Y-%m-%d")

    print("=" * 50)
    print("🚀 알송 앱 리뷰 수집 시작")
    print(f"   기간: {start_s} ~ {end_s}")
    print("=" * 50)

    print("\n📱 [1/4] Google Play 리뷰 수집 중...")
    gp_reviews = fetch_google_play_reviews()
    print(f"   → {len(gp_reviews)}건 수집")

    print("\n🍎 [2/4] App Store 리뷰 수집 중...")
    as_reviews = fetch_app_store_reviews()
    print(f"   → {len(as_reviews)}건 수집")

    all_reviews = sorted(gp_reviews + as_reviews, key=lambda x: x["date"], reverse=True)
    print(f"\n📊 총 {len(all_reviews)}건 수집 완료")

    print("\n🤖 [3/4] Claude AI 분석 중...")
    summary = summarize_with_claude(all_reviews)
    print("   → AI 요약 완료")

    print("\n📧 [4/4] HTML 리포트 생성 및 이메일 발송...")
    html = create_html_report(all_reviews, summary, start_s, end_s)

    month_range = f"{cutoff.strftime('%m/%d')}~{now.strftime('%m/%d')}"
    subject = f"[알송] 주간 리뷰 리포트 ({month_range}) — 총 {len(all_reviews)}건"
    send_email(html, subject)

    print("\n🎉 전체 완료!")


if __name__ == "__main__":
    main()
