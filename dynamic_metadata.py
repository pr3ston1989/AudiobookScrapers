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


def bookbeat_related(client, data):
    """Collect the public related-reading batch and retain its explicit pagination state."""
    from urllib.parse import urlsplit
    from recommendation_parsers import normalized_link

    source = data.get("related_readings_url")
    if not source:
        return
    key = identity("bookbeat", data["url"])
    path = f"/api/booklists/relatedreadings/Poland/{key}"
    try:
        parts = urlsplit(source)
        if parts.scheme != "https" or parts.netloc != "api.bookbeat.com" or parts.path != path:
            raise ValueError("BookBeat: adres rekomendacji dotyczy innej książki")
        get = getattr(client, "get_optional", client.get)
        url = f"https://api.bookbeat.com{path}?offset=0&limit=25&includeerotic=false"
        value = json.loads(get(url).text)
        if not isinstance(value, dict) or str(value.get("id")) != key:
            raise ValueError("BookBeat: rekomendacje dotyczą innej książki")
        books = (value.get("_embedded") or {}).get("books")
        if not isinstance(books, list):
            raise ValueError("BookBeat: brak listy rekomendacji")
        links = []
        seen = set()
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
        data["related_links"] = links
        data["related_urls"] = [item["url"] for item in links]
        data["recommendation_coverage"] = {"scope": "first_api_batch", "complete": not bool(next_url), "collected": len(links),
                                            "next_url": next_url, "source_url": url}
    except Exception as exc:
        data["recommendation_coverage"] = {"complete": False, "error": str(exc)}
