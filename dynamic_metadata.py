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
