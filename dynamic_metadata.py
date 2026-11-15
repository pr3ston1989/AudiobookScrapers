"""Read the public review batch that BookBeat loads separately from its product HTML."""
import json

from urls import identity


def bookbeat_reviews(client, data):
    """Keep the newest public batch and explicitly describe remaining review pagination."""
    key = identity('bookbeat', data['url'])
    url = f'https://api.bookbeat.com/api/reviews/pl/book/{key}?limit=4&offset=0&sortby=createdat&sortorder=desc'
    try:
        get = getattr(client, 'get_optional', client.get)
        value = json.loads(get(url).text)
        if not isinstance(value, dict) or not isinstance(value.get('_embedded'), dict):
            raise ValueError('BookBeat: nieprawidłowa odpowiedź opinii')
        reviews = value['_embedded'].get('reviews')
        if not isinstance(reviews, list):
            raise ValueError('BookBeat: brak listy opinii w odpowiedzi')
        parsed = []
        for review in reviews:
            if str(review.get('bookid')) != key:
                raise ValueError('BookBeat: opinia dotyczy innej książki')
            if review.get('reviewtext'):
                parsed.append({'username': review.get('reviewername') or 'Anonim', 'text': review['reviewtext'],
                               'date': review.get('createdat'), 'rating': review.get('bookrating'),
                               'source_review_id': review.get('id'), 'narration_rating': review.get('narrationrating'),
                               'upvotes': review.get('upvotecount')})
        next_url = ((value.get('_links') or {}).get('next') or {}).get('href')
        data['reviews'] = parsed
        data['review_coverage'] = {'complete': not next_url, 'scope': 'newest_public_batch', 'fetched': len(parsed), 'next_url': next_url}
    except Exception as exc:
        data['review_coverage'] = {'complete': False, 'scope': 'newest_public_batch', 'error': str(exc)}


def bookbeat_related(client, data, *, all_pages=False):
    """Validate each public recommendation page before exposing any collected relationships."""
    from urllib.parse import urlsplit, parse_qs
    from recommendation_parsers import normalized_link

    source = data.get("related_readings_url")
    if not source:
        return
    key = identity("bookbeat", data["url"])
    path = f"/api/booklists/relatedreadings/Poland/{key}"

    def valid_url(value):
        parts = urlsplit(value)
        query = parse_qs(parts.query)
        if (parts.scheme != "https" or parts.netloc != "api.bookbeat.com" or parts.path != path
                or not query.get("offset", [""])[0].isdigit()
                or not query.get("limit", [""])[0].isdigit()):
            raise ValueError("BookBeat: nieprawidłowy adres partii rekomendacji")
        return value

    try:
        valid_url(source)
        get = getattr(client, "get_optional", client.get)
        url = f"https://api.bookbeat.com{path}?offset=0&limit=25&includeerotic=false"
        first_url = url
        links, seen, pages = [], set(), set()
        next_url = None
        while url:
            valid_url(url)
            if url in pages or len(pages) >= 100:
                raise ValueError("BookBeat: zapętlona lub nieograniczona paginacja rekomendacji")
            pages.add(url)
            value = json.loads(get(url).text)
            if not isinstance(value, dict) or str(value.get("id")) != key:
                raise ValueError("BookBeat: rekomendacje dotyczą innej książki")
            books = (value.get("_embedded") or {}).get("books")
            if not isinstance(books, list):
                raise ValueError("BookBeat: brak listy rekomendacji")
            before = len(links)
            for book in books:
                if not isinstance(book, dict) or not str(book.get("id", "")).isdigit():
                    raise ValueError("BookBeat: brak identyfikatora rekomendacji")
                target = str(book["id"])
                href = book.get("shareurl") or f"https://www.bookbeat.com/pl/book/{target}"
                if identity("bookbeat", href) != target:
                    raise ValueError("BookBeat: niespójny identyfikator rekomendacji")
                item = normalized_link("bookbeat", {"url": href, "title": book.get("title"),
                    "relation_kind": "also_liked", "source_heading": value.get("title") or "Inni też lubią",
                    "audio_available": book.get("audio")})
                if item and target != key and target not in seen:
                    links.append(item)
                    seen.add(target)
            next_url = ((value.get("_links") or {}).get("next") or {}).get("href")
            if next_url:
                valid_url(next_url)
            if not all_pages or not next_url:
                break
            if len(links) == before:
                raise ValueError("BookBeat: kolejna partia nie wnosi żadnych rekomendacji")
            url = next_url
        data["related_links"] = links
        data["related_urls"] = [item["url"] for item in links]
        data["recommendation_coverage"] = {"scope": "all_api_pages" if all_pages else "first_api_batch",
            "complete": not bool(next_url), "collected": len(links), "pages": len(pages),
            "next_url": next_url, "source_url": first_url}
    except Exception as exc:
        data["recommendation_coverage"] = {"complete": False, "error": str(exc)}
