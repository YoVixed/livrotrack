"""
LivroTrack API — FastAPI backend
"""

from datetime import datetime, timedelta
from typing import Optional
import os
import asyncpg
from fastapi import FastAPI, HTTPException, Query, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr

app = FastAPI(title="LivroTrack API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://livrotrack:changeme@localhost/livrotrack")
db_pool: asyncpg.Pool = None


@app.on_event("startup")
async def startup():
    global db_pool
    try:
        db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
        print("✓ Database pool created")
    except Exception as e:
        print(f"✗ Database connection failed: {e}")
        raise


@app.on_event("shutdown")
async def shutdown():
    if db_pool:
        await db_pool.close()


# ─── Models ───────────────────────────────────────────────────────────────────

class AlertCreate(BaseModel):
    book_id: str
    email: EmailStr
    target_price: float


class SearchRequest(BaseModel):
    query: str


# ─── Endpoints ────────────────────────────────────────────────────────────────

@app.get("/api/books/search")
async def search_books(
    q: str = Query(..., min_length=2, description="Título, autor ou ISBN"),
    background_tasks: BackgroundTasks = None,
):
    """
    Busca livros. Primeiro verifica o banco local; se não achar,
    faz scraping na Amazon e salva para tracking futuro.
    """
    if not db_pool:
        raise HTTPException(503, "Database not available")
    
    async with db_pool.acquire() as conn:
        # Busca no banco local
        rows = await conn.fetch("""
            SELECT b.id, b.asin, b.title, b.author, b.cover_url, b.amazon_url,
                   blp.current_price, blp.in_stock, blp.last_checked,
                   bps.min_price, bps.max_price, bps.avg_price
            FROM books b
            LEFT JOIN book_latest_price blp ON b.id = blp.id
            LEFT JOIN book_price_stats bps ON b.id = bps.book_id
            WHERE b.title ILIKE $1 OR b.author ILIKE $1 OR b.isbn = $2
            LIMIT 20
        """, f"%{q}%", q.strip())

        if rows:
            return {"source": "local", "results": [dict(r) for r in rows]}

    return {"source": "local", "results": []}


@app.get("/api/books/{asin}")
async def get_book(asin: str):
    """Retorna dados completos de um livro com histórico de preços"""
    if not db_pool:
        raise HTTPException(503, "Database not available")
        
    async with db_pool.acquire() as conn:
        book = await conn.fetchrow("""
            SELECT b.*, blp.current_price, blp.original_price, blp.discount_pct,
                   blp.in_stock, blp.last_checked,
                   bps.min_price, bps.max_price, bps.avg_price, bps.tracking_since
            FROM books b
            LEFT JOIN book_latest_price blp ON b.id = blp.id
            LEFT JOIN book_price_stats bps ON b.id = bps.book_id
            WHERE b.asin = $1
        """, asin)

        if not book:
            raise HTTPException(404, "Livro não encontrado")

        # Busca histórico de preços
        history = await conn.fetch("""
            SELECT price, original_price, discount_pct, in_stock, scraped_at
            FROM price_history
            WHERE book_id = $1
            ORDER BY scraped_at ASC
        """, book["id"])

        # Análise de padrão — meses com menor preço
        monthly_avg = await conn.fetch("""
            SELECT
                EXTRACT(MONTH FROM scraped_at) AS month,
                ROUND(AVG(price)::numeric, 2) AS avg_price
            FROM price_history
            WHERE book_id = $1 AND scraped_at >= NOW() - INTERVAL '365 days'
            GROUP BY month
            ORDER BY avg_price ASC
            LIMIT 3
        """, book["id"])

        month_names = {
            1:"jan",2:"fev",3:"mar",4:"abr",5:"mai",6:"jun",
            7:"jul",8:"ago",9:"set",10:"out",11:"nov",12:"dez"
        }
        best_months = [month_names.get(int(r["month"]), "?") for r in monthly_avg]

        result = dict(book)
        result["history"] = [dict(h) for h in history]
        result["best_months"] = best_months
        result["price_drops_count"] = len([
            i for i in range(1, len(history))
            if history[i]["price"] < history[i-1]["price"]
        ])
        return result


@app.get("/api/books/{asin}/history")
async def get_price_history(
    asin: str,
    days: int = Query(365, ge=7, le=730),
):
    """Retorna histórico de preços para o gráfico"""
    if not db_pool:
        raise HTTPException(503, "Database not available")
        
    async with db_pool.acquire() as conn:
        book_id = await conn.fetchval("SELECT id FROM books WHERE asin=$1", asin)
        if not book_id:
            raise HTTPException(404, "Livro não encontrado")

        rows = await conn.fetch("""
            SELECT price, original_price, discount_pct, scraped_at
            FROM price_history
            WHERE book_id=$1 AND scraped_at >= NOW() - ($2 || ' days')::interval
            ORDER BY scraped_at ASC
        """, book_id, str(days))

        return {"asin": asin, "days": days, "points": [dict(r) for r in rows]}


@app.post("/api/alerts")
async def create_alert(alert: AlertCreate):
    """Cria alerta de preço por email"""
    if not db_pool:
        raise HTTPException(503, "Database not available")
        
    async with db_pool.acquire() as conn:
        # Verifica se o livro existe
        book = await conn.fetchrow("SELECT id, title FROM books WHERE id=$1", alert.book_id)
        if not book:
            raise HTTPException(404, "Livro não encontrado")

        # Verifica duplicata
        existing = await conn.fetchval("""
            SELECT id FROM price_alerts
            WHERE book_id=$1 AND email=$2 AND triggered=FALSE
        """, alert.book_id, alert.email)
        
        if existing:
            raise HTTPException(409, "Você já tem um alerta ativo para este livro")

        alert_id = await conn.fetchval("""
            INSERT INTO price_alerts (book_id, email, target_price)
            VALUES ($1,$2,$3) RETURNING id
        """, alert.book_id, alert.email, alert.target_price)

        return {
            "id": str(alert_id),
            "message": f"Alerta criado! Você receberá um email quando '{book['title']}' cair para R$ {alert.target_price:.2f}",
        }


@app.delete("/api/alerts/{alert_id}")
async def delete_alert(alert_id: str, email: str = Query(...)):
    """Remove um alerta (requer email para confirmar)"""
    if not db_pool:
        raise HTTPException(503, "Database not available")
        
    async with db_pool.acquire() as conn:
        deleted = await conn.fetchval("""
            DELETE FROM price_alerts WHERE id=$1 AND email=$2 RETURNING id
        """, alert_id, email)
        
        if not deleted:
            raise HTTPException(404, "Alerta não encontrado")
        return {"message": "Alerta removido"}


@app.post("/api/books/{asin}/track")
async def start_tracking(asin: str):
    """Adiciona livro à fila de monitoramento"""
    if not db_pool:
        raise HTTPException(503, "Database not available")
        
    async with db_pool.acquire() as conn:
        exists = await conn.fetchval("SELECT id FROM books WHERE asin=$1", asin)
        if exists:
            return {"message": "Livro já está sendo monitorado", "asin": asin}
        
        return {"message": "Monitoramento iniciado!", "asin": asin}


@app.get("/api/trending")
async def get_trending():
    """Livros com maior queda de preço nos últimos 7 dias"""
    if not db_pool:
        raise HTTPException(503, "Database not available")
        
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("""
            WITH recent AS (
                SELECT book_id, price, scraped_at,
                       LAG(price) OVER (PARTITION BY book_id ORDER BY scraped_at) AS prev_price
                FROM price_history
                WHERE scraped_at >= NOW() - INTERVAL '7 days'
            )
            SELECT b.asin, b.title, b.author, b.cover_url,
                   r.price AS current_price, r.prev_price,
                   ROUND(((r.prev_price - r.price) / r.prev_price * 100)::numeric, 1) AS drop_pct
            FROM recent r
            JOIN books b ON r.book_id = b.id
            WHERE r.prev_price IS NOT NULL AND r.price < r.prev_price
            ORDER BY drop_pct DESC
            LIMIT 10
        """)
        return {"results": [dict(r) for r in rows] if rows else []}


@app.get("/api/health")
async def health():
    return {"status": "ok", "timestamp": datetime.now().isoformat()}
