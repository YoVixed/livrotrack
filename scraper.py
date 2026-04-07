"""
LivroTrack Scraper
Coleta preços de livros na Amazon.com.br
"""

import asyncio
import random
import re
import logging
from datetime import datetime
from typing import Optional
from dataclasses import dataclass

import httpx
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# Rotação de User-Agents para evitar bloqueios
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_3) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
]


@dataclass
class BookData:
    asin: str
    title: str
    author: Optional[str]
    publisher: Optional[str]
    isbn: Optional[str]
    cover_url: Optional[str]
    amazon_url: str
    current_price: Optional[float]
    original_price: Optional[float]
    discount_pct: Optional[int]
    in_stock: bool


@dataclass
class SearchResult:
    asin: str
    title: str
    author: Optional[str]
    cover_url: Optional[str]
    price: Optional[float]
    amazon_url: str


def get_headers() -> dict:
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Cache-Control": "max-age=0",
    }


def parse_price(text: str) -> Optional[float]:
    """Converte 'R$ 89,90' → 89.90"""
    if not text:
        return None
    cleaned = re.sub(r"[^\d,]", "", text.strip())
    cleaned = cleaned.replace(",", ".")
    # Se tiver mais de um ponto, remove os de milhar
    parts = cleaned.split(".")
    if len(parts) > 2:
        cleaned = "".join(parts[:-1]) + "." + parts[-1]
    try:
        return float(cleaned)
    except ValueError:
        return None


def extract_asin_from_url(url: str) -> Optional[str]:
    patterns = [
        r"/dp/([A-Z0-9]{10})",
        r"/gp/product/([A-Z0-9]{10})",
        r"ASIN=([A-Z0-9]{10})",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


class AmazonScraper:
    BASE_URL = "https://www.amazon.com.br"
    
    def __init__(self, proxy: Optional[str] = None):
        self.proxy = proxy
        
    def _make_client(self) -> httpx.AsyncClient:
        kwargs = {
            "headers": get_headers(),
            "timeout": httpx.Timeout(30.0),
            "follow_redirects": True,
        }
        if self.proxy:
            kwargs["proxies"] = {"https://": self.proxy, "http://": self.proxy}
        return httpx.AsyncClient(**kwargs)

    async def _get(self, url: str, retries: int = 3) -> Optional[str]:
        for attempt in range(retries):
            try:
                async with self._make_client() as client:
                    # Delay aleatório para não sobrecarregar
                    await asyncio.sleep(random.uniform(2.0, 5.0))
                    resp = await client.get(url)
                    
                    if resp.status_code == 200:
                        return resp.text
                    elif resp.status_code == 503:
                        log.warning(f"Amazon retornou 503, tentativa {attempt+1}/{retries}")
                        await asyncio.sleep(random.uniform(10, 20))
                    elif resp.status_code == 404:
                        log.error(f"Produto não encontrado: {url}")
                        return None
                    else:
                        log.warning(f"Status {resp.status_code} para {url}")
            except httpx.TimeoutException:
                log.warning(f"Timeout na tentativa {attempt+1}/{retries}")
                await asyncio.sleep(5)
            except Exception as e:
                log.error(f"Erro ao buscar {url}: {e}")
                await asyncio.sleep(5)
        return None

    async def get_book_by_asin(self, asin: str) -> Optional[BookData]:
        url = f"{self.BASE_URL}/dp/{asin}"
        html = await self._get(url)
        if not html:
            return None
        return self._parse_product_page(html, asin, url)

    def _parse_product_page(self, html: str, asin: str, url: str) -> Optional[BookData]:
        soup = BeautifulSoup(html, "html.parser")
        
        # Detecta CAPTCHA
        if "captcha" in html.lower() and len(html) < 5000:
            log.warning("CAPTCHA detectado!")
            return None

        # Título
        title_el = soup.find("span", id="productTitle")
        title = title_el.get_text(strip=True) if title_el else None
        if not title:
            log.warning(f"Título não encontrado para ASIN {asin}")
            return None

        # Autor
        author = None
        author_el = soup.find("span", class_="author")
        if author_el:
            a_tag = author_el.find("a")
            author = a_tag.get_text(strip=True) if a_tag else author_el.get_text(strip=True)

        # Preço principal
        current_price = None
        price_selectors = [
            ("span", {"class": "a-price-whole"}),
            ("span", {"id": "price_inside_buybox"}),
            ("span", {"id": "kindle-price"}),
            ("span", {"class": "a-offscreen"}),
        ]
        
        for tag, attrs in price_selectors:
            el = soup.find(tag, attrs)
            if el:
                price_text = el.get_text(strip=True)
                current_price = parse_price(price_text)
                if current_price and current_price > 0:
                    break

        # Preço original (antes do desconto)
        original_price = None
        orig_el = soup.find("span", {"class": "a-text-strike"})
        if orig_el:
            original_price = parse_price(orig_el.get_text())

        # % de desconto
        discount_pct = None
        if current_price and original_price and original_price > 0:
            discount_pct = int(round((1 - current_price / original_price) * 100))

        # Disponibilidade
        in_stock = True
        avail_el = soup.find("div", id="availability")
        if avail_el:
            avail_text = avail_el.get_text(strip=True).lower()
            if "indisponível" in avail_text or "esgotado" in avail_text or "unavailable" in avail_text:
                in_stock = False

        # Imagem de capa
        cover_url = None
        img_el = soup.find("img", id="landingImage") or soup.find("img", id="imgBlkFront")
        if img_el:
            cover_url = img_el.get("src") or img_el.get("data-a-dynamic-image", "").split('"')[1] if img_el else None

        # ISBN e editora dos detalhes do produto
        isbn = None
        publisher = None
        detail_lists = soup.find_all("li", {"class": "a-spacing-small"})
        for li in detail_lists:
            text = li.get_text()
            if "ISBN-13" in text or "ISBN-10" in text:
                isbn = re.search(r"[\d-]{10,17}", text)
                isbn = isbn.group() if isbn else None
            if "Editora" in text or "Publisher" in text:
                pub_match = re.search(r"(?:Editora|Publisher)[:\s]+([^\(;]+)", text)
                if pub_match:
                    publisher = pub_match.group(1).strip()

        return BookData(
            asin=asin,
            title=title,
            author=author,
            publisher=publisher,
            isbn=isbn,
            cover_url=cover_url,
            amazon_url=url,
            current_price=current_price,
            original_price=original_price,
            discount_pct=discount_pct,
            in_stock=in_stock,
        )

    async def search_books(self, query: str, max_results: int = 10) -> list[SearchResult]:
        """Busca livros por título/autor na Amazon.com.br"""
        search_url = f"{self.BASE_URL}/s?k={query.replace(' ', '+')}&i=stripbooks&language=pt_BR"
        html = await self._get(search_url)
        if not html:
            return []
        return self._parse_search_results(html, max_results)

    def _parse_search_results(self, html: str, max_results: int) -> list[SearchResult]:
        soup = BeautifulSoup(html, "html.parser")
        results = []

        items = soup.find_all("div", {"data-component-type": "s-search-result"})
        for item in items[:max_results]:
            asin = item.get("data-asin")
            if not asin:
                continue

            title_el = item.find("span", {"class": "a-text-normal"})
            title = title_el.get_text(strip=True) if title_el else None
            if not title:
                continue

            author_el = item.find("span", {"class": "a-size-base"})
            author = author_el.get_text(strip=True) if author_el else None

            price_el = item.find("span", {"class": "a-offscreen"})
            price = parse_price(price_el.get_text()) if price_el else None

            img_el = item.find("img", {"class": "s-image"})
            cover_url = img_el.get("src") if img_el else None

            results.append(SearchResult(
                asin=asin,
                title=title,
                author=author,
                cover_url=cover_url,
                price=price,
                amazon_url=f"https://www.amazon.com.br/dp/{asin}",
            ))

        return results


# ─── Scheduler de coleta ───────────────────────────────────────────────────────

async def scrape_and_save(asin: str, db_conn, scraper: AmazonScraper):
    """Coleta preço de um livro e salva no banco"""
    book = await scraper.get_book_by_asin(asin)
    if not book:
        log.warning(f"Falha ao coletar ASIN {asin}")
        return

    # Upsert do livro
    await db_conn.execute("""
        INSERT INTO books (asin, title, author, publisher, isbn, cover_url, amazon_url)
        VALUES ($1,$2,$3,$4,$5,$6,$7)
        ON CONFLICT (asin) DO UPDATE SET
          title=EXCLUDED.title, author=EXCLUDED.author,
          publisher=EXCLUDED.publisher, cover_url=EXCLUDED.cover_url,
          updated_at=NOW()
    """, book.asin, book.title, book.author, book.publisher,
        book.isbn, book.cover_url, book.amazon_url)

    # Salva preço apenas se mudou
    last = await db_conn.fetchrow("""
        SELECT price FROM price_history
        WHERE book_id = (SELECT id FROM books WHERE asin=$1)
        ORDER BY scraped_at DESC LIMIT 1
    """, asin)

    if not last or last["price"] != book.current_price:
        await db_conn.execute("""
            INSERT INTO price_history (book_id, price, original_price, discount_pct, in_stock)
            SELECT id, $2, $3, $4, $5 FROM books WHERE asin=$1
        """, asin, book.current_price, book.original_price,
            book.discount_pct, book.in_stock)
        log.info(f"Novo preço registrado: {book.title} → R$ {book.current_price}")

        # Verifica alertas
        await check_alerts(asin, book.current_price, db_conn)
    else:
        log.info(f"Preço sem mudança: {book.title} (R$ {book.current_price})")


async def check_alerts(asin: str, current_price: float, db_conn):
    """Dispara alertas de preço por email"""
    alerts = await db_conn.fetch("""
        SELECT pa.id, pa.email, pa.target_price, b.title
        FROM price_alerts pa
        JOIN books b ON pa.book_id = b.id
        WHERE b.asin=$1 AND pa.triggered=FALSE AND $2 <= pa.target_price
    """, asin, current_price)

    for alert in alerts:
        log.info(f"Alerta disparado: {alert['email']} | {alert['title']} → R$ {current_price}")
        # Aqui chamaria o serviço de email (Resend/SendGrid)
        # await send_alert_email(alert['email'], alert['title'], current_price, alert['target_price'])
        
        await db_conn.execute("""
            UPDATE price_alerts SET triggered=TRUE, triggered_at=NOW() WHERE id=$1
        """, alert["id"])


async def run_scraper_loop(asins: list[str], interval_hours: int = 6):
    """Loop principal do scraper — roda a cada N horas"""
    import asyncpg
    import os
    
    db = await asyncpg.connect(os.getenv("DATABASE_URL", "postgresql://localhost/livrotrack"))
    scraper = AmazonScraper(proxy=os.getenv("PROXY_URL"))

    while True:
        log.info(f"Iniciando ciclo de coleta para {len(asins)} livros...")
        for asin in asins:
            await scrape_and_save(asin, db, scraper)
            # Pausa entre livros para não parecer um bot
            await asyncio.sleep(random.uniform(8, 15))
        
        next_run = datetime.now().strftime("%H:%M")
        log.info(f"Ciclo completo. Próxima coleta em {interval_hours}h.")
        await asyncio.sleep(interval_hours * 3600)


if __name__ == "__main__":
    # Teste rápido
    async def test():
        scraper = AmazonScraper()
        results = await scraper.search_books("Harry Potter português")
        for r in results[:3]:
            print(f"  {r.title} | {r.author} | R$ {r.price} | {r.asin}")
        
        if results:
            book = await scraper.get_book_by_asin(results[0].asin)
            if book:
                print(f"\nDetalhes: {book.title}")
                print(f"  Preço: R$ {book.current_price}")
                print(f"  Editora: {book.publisher}")
                print(f"  ISBN: {book.isbn}")
    
    asyncio.run(test())
