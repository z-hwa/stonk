"""
持倉管理 Web UI (FastAPI)
  - GET  /         → HTML 表格 + 表單
  - POST /add      → 新增/更新持倉
  - POST /remove   → 刪除持倉
  - GET  /healthz  → Cloud Run 健康檢查

認證 (擇一):
  POSITIONS_TOKEN=xxxxx  → URL 加 ?token=xxxxx 或 header Authorization: Bearer xxxxx
  未設定                 → 開放(僅本機測試用)

Cloud Run 部署:
  POSITIONS_BACKEND=gcs
  POSITIONS_GCS_BUCKET=your-bucket
  POSITIONS_TOKEN=long-random-string
"""
import os
from datetime import date

from fastapi import FastAPI, Form, HTTPException, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from dotenv import load_dotenv

from positions_store import get_store

load_dotenv()

app = FastAPI(title="Stonk Positions")
store = get_store()

TOKEN = os.getenv("POSITIONS_TOKEN", "").strip()


def _check_token(request: Request):
    if not TOKEN:
        return  # 未設 token = 不檢查 (本機開發)
    # Header
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer ") and auth[7:] == TOKEN:
        return
    # Query string
    if request.query_params.get("token") == TOKEN:
        return
    raise HTTPException(status_code=401, detail="invalid or missing token")


def _token_qs() -> str:
    """form action 帶上 token,POST 後仍能維持登入"""
    return f"?token={TOKEN}" if TOKEN else ""


PAGE = """<!doctype html>
<html lang="zh-Hant"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Stonk Positions</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: -apple-system, system-ui, sans-serif; max-width: 720px;
         margin: 1.5rem auto; padding: 0 1rem; }}
  h1 {{ font-size: 1.3rem; margin: 0 0 1rem; }}
  table {{ width: 100%; border-collapse: collapse; margin-bottom: 1.5rem; }}
  th, td {{ padding: .5rem .6rem; border-bottom: 1px solid #8884; text-align: left; }}
  th {{ font-size: .85rem; opacity: .7; font-weight: 500; }}
  td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  form.inline {{ display: inline; }}
  button {{ padding: .35rem .7rem; border: 1px solid #8886; background: transparent;
           border-radius: 6px; cursor: pointer; }}
  button:hover {{ background: #8882; }}
  button.danger {{ color: #c33; }}
  .add {{ display: grid; grid-template-columns: 1fr 1fr 1fr auto; gap: .5rem;
         padding: 1rem; border: 1px solid #8884; border-radius: 8px; }}
  .add input {{ padding: .5rem; border: 1px solid #8886; border-radius: 6px;
               background: transparent; color: inherit; font: inherit; }}
  .add button {{ background: #2563eb; color: white; border: none; }}
  .empty {{ opacity: .6; padding: 2rem 0; text-align: center; }}
  .meta {{ font-size: .8rem; opacity: .6; margin-top: 1rem; }}
</style>
</head><body>
<h1>📈 Stonk Positions ({count})</h1>

{table_html}

<form class="add" method="post" action="/add{token_qs}">
  <input name="symbol" placeholder="Symbol (e.g. AAPL)" required pattern="[A-Za-z]+" autocapitalize="characters">
  <input name="entry_price" type="number" step="0.01" min="0.01" placeholder="Entry price" required>
  <input name="entry_date" type="date" value="{today}" required>
  <button type="submit">Add / Update</button>
</form>

<p class="meta">backend: {backend}</p>
</body></html>"""


def _render(positions: dict) -> str:
    if positions:
        rows = []
        for sym in sorted(positions.keys()):
            p = positions[sym]
            rows.append(
                "<tr>"
                f"<td><strong>{sym}</strong></td>"
                f"<td class='num'>${float(p.get('entry_price', 0)):.2f}</td>"
                f"<td>{p.get('entry_date', '-')}</td>"
                "<td style='text-align:right'>"
                f"<form class='inline' method='post' action='/remove{_token_qs()}' "
                f"onsubmit=\"return confirm('Remove {sym}?')\">"
                f"<input type='hidden' name='symbol' value='{sym}'>"
                "<button class='danger' type='submit'>×</button>"
                "</form></td>"
                "</tr>"
            )
        table_html = (
            "<table><thead><tr>"
            "<th>Symbol</th><th class='num'>Entry</th><th>Date</th><th></th>"
            "</tr></thead><tbody>"
            + "".join(rows)
            + "</tbody></table>"
        )
    else:
        table_html = "<div class='empty'>(no positions)</div>"

    return PAGE.format(
        count=len(positions),
        table_html=table_html,
        token_qs=_token_qs(),
        today=date.today().isoformat(),
        backend=type(store).__name__,
    )


@app.get("/", response_class=HTMLResponse)
def index(request: Request, _=Depends(_check_token)):
    return _render(store.load())


@app.post("/add")
def add(
    request: Request,
    symbol: str = Form(...),
    entry_price: float = Form(...),
    entry_date: str = Form(...),
    _=Depends(_check_token),
):
    if entry_price <= 0:
        raise HTTPException(400, "entry_price must be > 0")
    store.add(symbol.strip().upper(), entry_price, entry_date.strip())
    return RedirectResponse(url=f"/{_token_qs()}", status_code=303)


@app.post("/remove")
def remove(
    request: Request,
    symbol: str = Form(...),
    _=Depends(_check_token),
):
    store.remove(symbol.strip().upper())
    return RedirectResponse(url=f"/{_token_qs()}", status_code=303)


@app.get("/healthz")
def healthz():
    return {"ok": True, "backend": type(store).__name__}


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port)
