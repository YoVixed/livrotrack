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

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://localhost/livrotrack")
db_pool: asyncpg.Pool = None


@app.on_event("startup")
async def startup():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)


@app.on_event("shutdown")
async def shutdown():
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

    # Se não achou localmente, busca na Amazon em background
    from scraper.scraper import AmazonScraper
    scraper = AmazonScraper()
    results = await scraper.search_books(q, max_results=10)
    
    return {
        "source": "amazon",
        "results": [
            {
                "asin": r.asin,
                "title": r.title,
                "author": r.author,
                "cover_url": r.cover_url,
                "current_price": r.price,
                "amazon_url": r.amazon_url,
                "tracked": False,
            }
            for r in results
        ],
    }


@app.get("/api/books/{asin}")
async def get_book(asin: str):
    """Retorna dados completos de um livro com histórico de preços"""
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
            # Não está no banco, faz scraping agora
            from scraper.scraper import AmazonScraper
            scraper = AmazonScraper()
            scraped = await scraper.get_book_by_asin(asin)
            if not scraped:
                raise HTTPException(404, "Livro não encontrado")
            
            # Salva no banco para tracking futuro
            book_id = await conn.fetchval("""
                INSERT INTO books (asin, title, author, publisher, isbn, cover_url, amazon_url)
                VALUES ($1,$2,$3,$4,$5,$6,$7)
                ON CONFLICT (asin) DO UPDATE SET title=EXCLUDED.title
                RETURNING id
            """, scraped.asin, scraped.title, scraped.author, scraped.publisher,
                scraped.isbn, scraped.cover_url, scraped.amazon_url)
            
            if scraped.current_price:
                await conn.execute("""
                    INSERT INTO price_history (book_id, price, original_price, discount_pct, in_stock)
                    VALUES ($1,$2,$3,$4,$5)
                """, book_id, scraped.current_price, scraped.original_price,
                    scraped.discount_pct, scraped.in_stock)
            
            return {
                "asin": scraped.asin,
                "title": scraped.title,
                "author": scraped.author,
                "publisher": scraped.publisher,
                "isbn": scraped.isbn,
                "cover_url": scraped.cover_url,
                "amazon_url": scraped.amazon_url,
                "current_price": scraped.current_price,
                "original_price": scraped.original_price,
                "discount_pct": scraped.discount_pct,
                "in_stock": scraped.in_stock,
                "tracking_since": datetime.now().isoformat(),
                "history": [],
            }

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
    from scraper.scraper import AmazonScraper
    async with db_pool.acquire() as conn:
        exists = await conn.fetchval("SELECT id FROM books WHERE asin=$1", asin)
        if exists:
            return {"message": "Livro já está sendo monitorado", "asin": asin}
        
        scraper = AmazonScraper()
        book = await scraper.get_book_by_asin(asin)
        if not book:
            raise HTTPException(400, "Não foi possível coletar dados deste livro")

        book_id = await conn.fetchval("""
            INSERT INTO books (asin, title, author, publisher, isbn, cover_url, amazon_url)
            VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING id
        """, book.asin, book.title, book.author, book.publisher,
            book.isbn, book.cover_url, book.amazon_url)
        
        if book.current_price:
            await conn.execute("""
                INSERT INTO price_history (book_id, price, original_price, discount_pct, in_stock)
                VALUES ($1,$2,$3,$4,$5)
            """, book_id, book.current_price, book.original_price,
                book.discount_pct, book.in_stock)

        return {"message": "Monitoramento iniciado!", "asin": asin, "title": book.title}


@app.get("/api/trending")
async def get_trending():
    """Livros com maior queda de preço nos últimos 7 dias"""
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
        return {"results": [dict(r) for r in rows]}


@app.get("/api/health")
async def health():
    return {"status": "ok", "timestamp": datetime.now().isoformat()}
