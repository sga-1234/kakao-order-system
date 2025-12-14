from fastapi import FastAPI, Request
from pydantic import BaseModel
from typing import Optional, Dict, Any
from datetime import datetime
import sqlite3
import os
import re

DB_PATH = "orders.db"

app = FastAPI(title="Kakao Order System")


# =========================
# DB 유틸
# =========================
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    cur = conn.cursor()

    # 상품 테이블
    cur.execute("""
    CREATE TABLE IF NOT EXISTS products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        price INTEGER NOT NULL,
        base_stock INTEGER NOT NULL DEFAULT 0,
        is_active INTEGER NOT NULL DEFAULT 1
    )
    """)

    # 주문 테이블
    cur.execute("""
    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        phone4 TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """)

    # 주문 상세 테이블
    cur.execute("""
    CREATE TABLE IF NOT EXISTS order_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER NOT NULL,
        product_id INTEGER NOT NULL,
        quantity INTEGER NOT NULL,
        unit_price INTEGER NOT NULL,
        FOREIGN KEY(order_id) REFERENCES orders(id),
        FOREIGN KEY(product_id) REFERENCES products(id)
    )
    """)

    conn.commit()
    conn.close()


init_db()


# =========================
# Pydantic 모델 (관리자용 JSON API)
# =========================
class ProductCreate(BaseModel):
    name: str           # 상품 이름 (정확히 맞춰 써야 하는 이름)
    price: int          # 가격 (원)
    base_stock: int = 0 # 총 재고 수량


class ProductUpdate(BaseModel):
    name: Optional[str] = None
    price: Optional[int] = None
    base_stock: Optional[int] = None
    is_active: Optional[bool] = None


# =========================
# 헬퍼 함수들
# =========================
def normalize_name(name: str) -> str:
    """상품명 비교용: 공백 제거 + 양 끝 공백 제거"""
    return name.replace(" ", "").strip()


def parse_order_text(text: str):
    """
    주문 파싱 로직
    - 형식 예:
      "1234 콜라제로 2"
      "1234 콜 라 제 로 2개"
      "1234콜라제로2"
    - 상품명은 정확히 일치해야 하고, 띄어쓰기만 자유

    return: (phone4, product_candidate_normalized, quantity) 또는 None
    """
    raw = text.strip()

    # 1) 전화번호 뒷 4자리 찾기 (처음 등장하는 4자리 숫자)
    m_phone = re.search(r"(\d{4})", raw)
    if not m_phone:
        return None
    phone4 = m_phone.group(1)

    # 2) 마지막 숫자를 수량으로 (뒤에 '개' 있어도 허용)
    m_qty = re.search(r"(\d+)\s*개?\s*$", raw)
    if not m_qty:
        return None
    quantity = int(m_qty.group(1))

    # 3) 상품명 부분 뽑기: phone4, quantity, '개' 제거
    temp = raw
    # 앞쪽의 phone4 한 번만 제거
    temp = temp.replace(phone4, "", 1)
    # 맨 끝의 quantity(+개) 제거
    qty_pattern = re.compile(rf"{quantity}\s*개?\s*$")
    temp = qty_pattern.sub("", temp)

    # 남은 '개' 같은 거 혹시 있으면 제거
    temp = temp.replace("개", "")
    temp = temp.strip()

    if not temp:
        return None

    # 공백 제거 버전으로 비교
    product_candidate = normalize_name(temp)
    return phone4, product_candidate, quantity


def is_order_check_text(text: str) -> Optional[str]:
    """
    '1234 주문확인', '1234 주문 확인' 같은 형식 체크
    맞으면 phone4 반환, 아니면 None
    """
    raw = text.strip()
    parts = raw.split()
    if not parts:
        return None

    # 첫 단어가 4자리 숫자여야 함
    if re.fullmatch(r"\d{4}", parts[0]):
        phone4 = parts[0]
        rest = "".join(parts[1:])
        if "주문확인" in rest or ("주문" in rest and "확인" in rest):
            return phone4
    return None


def kakao_simple_text(text: str) -> Dict[str, Any]:
    """카카오 오픈빌더 simpleText 응답 포맷"""
    return {
        "version": "2.0",
        "template": {
            "outputs": [
                {
                    "simpleText": {
                        "text": text
                    }
                }
            ]
        }
    }


def build_order_summary_text(phone4: str) -> str:
    """특정 전화번호의 최근 주문 요약 텍스트 생성"""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT o.created_at,
               p.name AS product_name,
               i.quantity,
               i.unit_price
        FROM orders o
        JOIN order_items i ON o.id = i.order_id
        JOIN products p ON p.id = i.product_id
        WHERE o.phone4 = ?
        ORDER BY o.created_at DESC, o.id DESC
        LIMIT 50
    """, (phone4,))
    rows = cur.fetchall()
    conn.close()

    if not rows:
        return f"{phone4} 번호로 된 주문이 아직 없습니다."

    lines = [f"[{phone4}님의 최근 주문 목록]"]
    total_all = 0
    for r in rows:
        created = r["created_at"]
        name = r["product_name"]
        qty = r["quantity"]
        price = r["unit_price"]
        subtotal = qty * price
        total_all += subtotal
        lines.append(f"- {created} | {name} x{qty}개 = {subtotal}원")

    lines.append(f"\n최근 50건 기준 합계: {total_all}원")
    return "\n".join(lines)


def get_product_by_normalized_name(product_candidate: str):
    """
    상품명 찾기 (is_active=1만 대상)
    DB에 저장된 name에서 공백 제거 후 비교
    """
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM products WHERE is_active = 1")
    rows = cur.fetchall()
    for r in rows:
        if normalize_name(r["name"]) == product_candidate:
            conn.close()
            return r
    conn.close()
    return None


# =========================
# 기본 핑
# =========================
@app.get("/")
def root():
    return {"message": "Kakao order server running"}


# =========================
# 1. 카카오톡 주문 웹훅
# =========================
@app.post("/kakao/order")
async def kakao_order(request: Request):
    body = await request.json()
    user_text = body.get("userRequest", {}).get("utterance", "").strip()

    # (1) "1234 주문확인" 처리
    phone4_check = is_order_check_text(user_text)
    if phone4_check:
        summary = build_order_summary_text(phone4_check)
        return kakao_simple_text(summary)

    # (2) 일반 주문 파싱
    parsed = parse_order_text(user_text)
    if not parsed:
        help_msg = (
            "주문 형식이 올바르지 않습니다.\n\n"
            "예시)\n"
            "1234 콜라제로 2\n"
            "전화번호뒷4자리 상품이름 수량"
        )
        return kakao_simple_text(help_msg)

    phone4, product_candidate, quantity = parsed

    # (3) 상품 찾기 (이름 정확히 맞추되 공백 무시는 허용)
    product = get_product_by_normalized_name(product_candidate)
    if not product:
        return kakao_simple_text(
            "정확한 상품명을 찾을 수 없습니다.\n"
            "공지에 적힌 상품 이름을 그대로 입력해 주세요."
        )

    product_id = product["id"]
    price = product["price"]

    # (4) 주문 기록
    conn = get_db()
    cur = conn.cursor()
    now = datetime.now().isoformat(timespec="seconds")
    cur.execute(
        "INSERT INTO orders (phone4, created_at) VALUES (?, ?)",
        (phone4, now)
    )
    order_id = cur.lastrowid
    cur.execute(
        """
        INSERT INTO order_items (order_id, product_id, quantity, unit_price)
        VALUES (?, ?, ?, ?)
        """,
        (order_id, product_id, quantity, price)
    )
    conn.commit()
    conn.close()

    total_price = price * quantity
    reply = (
        "[주문 완료]\n"
        f"번호: {phone4}\n"
        f"상품: {product['name']}\n"
        f"수량: {quantity}개\n"
        f"금액: {total_price}원\n\n"
        f"'{phone4} 주문확인' 을 보내면 지금까지 주문 목록을 볼 수 있어요."
    )
    return kakao_simple_text(reply)


# =========================
# 2. 상품 목록 웹페이지 (사용자용)
# =========================
@app.get("/products")
def show_products():
    """
    /products : 공지에 올려둘 상품 목록 링크용
    - is_active=1 인 상품만 노출
    - base_stock(총 재고), 주문 수량, 남은 수량 표시
    """
    conn = get_db()
    cur = conn.cursor()

    # active 상품
    cur.execute("SELECT * FROM products WHERE is_active = 1 ORDER BY id ASC")
    products = cur.fetchall()

    # 각 상품별 주문 수량 합계
    cur.execute("""
        SELECT p.id, p.name,
               p.price, p.base_stock,
               IFNULL(SUM(i.quantity), 0) AS ordered_qty
        FROM products p
        LEFT JOIN order_items i ON p.id = i.product_id
        GROUP BY p.id, p.name, p.price, p.base_stock
        ORDER BY p.id ASC
    """)
    summary_rows = cur.fetchall()
    conn.close()

    summary_map = {r["id"]: r for r in summary_rows}

    rows_html = ""
    for p in products:
        s = summary_map.get(p["id"])
        base_stock = s["base_stock"] if s else p["base_stock"]
        ordered = s["ordered_qty"] if s else 0
        remaining = base_stock - ordered if base_stock is not None else None
        status = "판매중"
        if base_stock is not None and base_stock > 0 and remaining <= 0:
            status = "품절"
        price = p["price"]

        rows_html += f"""
        <tr>
          <td>{p['name']}</td>
          <td>{price}원</td>
          <td>{base_stock}</td>
          <td>{ordered}</td>
          <td>{remaining}</td>
          <td>{status}</td>
        </tr>
        """

    from fastapi.responses import HTMLResponse
    html = f"""
    <!DOCTYPE html>
    <html lang="ko">
    <head>
      <meta charset="UTF-8" />
      <title>상품 목록</title>
      <style>
        body {{ font-family: sans-serif; padding: 20px; }}
        table {{ border-collapse: collapse; width: 100%; max-width: 800px; }}
        th, td {{ border: 1px solid #ccc; padding: 8px; text-align: center; }}
        th {{ background-color: #f5f5f5; }}
        h1 {{ margin-bottom: 10px; }}
        .desc {{ margin-bottom: 20px; color: #555; font-size: 14px; }}
      </style>
    </head>
    <body>
      <h1>상품 목록</h1>
      <div class="desc">
        공지된 상품 이름을 그대로 입력해서 주문해 주세요.<br/>
        예) <b>1234 콜라제로 2</b>
      </div>
      <table>
        <thead>
          <tr>
            <th>상품명</th>
            <th>가격</th>
            <th>총 재고</th>
            <th>주문된 수량</th>
            <th>남은 수량</th>
            <th>상태</th>
          </tr>
        </thead>
        <tbody>
          {rows_html}
        </tbody>
      </table>
    </body>
    </html>
    """
    return HTMLResponse(content=html)


# =========================
# 3. 관리자용 JSON API
# =========================

@app.get("/admin/products/summary")
def admin_product_summary():
    """상품별 재고/주문/잔여 수량 요약"""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT p.id, p.name, p.price, p.base_stock, p.is_active,
               IFNULL(SUM(i.quantity), 0) AS ordered_qty
        FROM products p
        LEFT JOIN order_items i ON p.id = i.product_id
        GROUP BY p.id, p.name, p.price, p.base_stock, p.is_active
        ORDER BY p.id ASC
    """)
    rows = cur.fetchall()
    conn.close()

    result = []
    for r in rows:
        base_stock = r["base_stock"]
        ordered = r["ordered_qty"]
        remaining = base_stock - ordered
        result.append({
            "id": r["id"],
            "name": r["name"],
            "price": r["price"],
            "base_stock": base_stock,
            "ordered_qty": ordered,
            "remaining": remaining,
            "is_active": bool(r["is_active"])
        })
    return result


@app.post("/admin/products")
def create_product(p: ProductCreate):
    """상품 추가 (이름, 가격, 총 재고)"""
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO products (name, price, base_stock, is_active) VALUES (?, ?, ?, 1)",
            (p.name, p.price, p.base_stock)
        )
        conn.commit()
        new_id = cur.lastrowid
    except sqlite3.IntegrityError:
        conn.close()
        return {"success": False, "message": "이미 존재하는 상품 이름입니다."}
    conn.close()
    return {"success": True, "id": new_id}


@app.get("/admin/products")
def list_products():
    """전체 상품 목록 (JSON)"""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM products ORDER BY id ASC")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


@app.patch("/admin/products/{product_id}")
def update_product(product_id: int, data: ProductUpdate):
    """
    상품 수정 (부분 수정)
    - name / price / base_stock / is_active 중 필요한 것만 보내면 됨
    """
    conn = get_db()
    cur = conn.cursor()

    updates = []
    params = []
    if data.name is not None:
        updates.append("name = ?")
        params.append(data.name)
    if data.price is not None:
        updates.append("price = ?")
        params.append(data.price)
    if data.base_stock is not None:
        updates.append("base_stock = ?")
        params.append(data.base_stock)
    if data.is_active is not None:
        updates.append("is_active = ?")
        params.append(1 if data.is_active else 0)

    if not updates:
        conn.close()
        return {"success": False, "message": "변경할 값이 없습니다."}

    params.append(product_id)
    sql = f"UPDATE products SET {', '.join(updates)} WHERE id = ?"
    cur.execute(sql, params)
    conn.commit()
    conn.close()
    return {"success": True}


@app.get("/admin/orders/by-phone/{phone4}")
def admin_orders_by_phone(phone4: str):
    """특정 전화번호의 주문 내역 JSON"""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT o.id AS order_id, o.phone4, o.created_at,
               p.name AS product_name,
               i.quantity, i.unit_price
        FROM orders o
        JOIN order_items i ON o.id = i.order_id
        JOIN products p ON p.id = i.product_id
        WHERE o.phone4 = ?
        ORDER BY o.created_at DESC, o.id DESC
    """, (phone4,))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


@app.get("/admin/orders/summary")
def admin_orders_summary():
    """상품별 총 주문 수량 / 매출 합계"""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT p.id, p.name,
               IFNULL(SUM(i.quantity), 0) AS total_qty,
               IFNULL(SUM(i.quantity * i.unit_price), 0) AS total_amount
        FROM products p
        LEFT JOIN order_items i ON p.id = i.product_id
        GROUP BY p.id, p.name
        ORDER BY p.id ASC
    """)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


# =========================
# 로컬 실행 / Render 실행
# =========================
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
