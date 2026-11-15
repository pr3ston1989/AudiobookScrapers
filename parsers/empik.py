"""Adapted from empik.com\\scraper\\parser.py; service-specific HTML parsing."""

import re
import json
from bs4 import BeautifulSoup
from typing import Dict, Any

class EmpikParser:
    def __init__(self, html: str, url: str):
        self.soup = BeautifulSoup(html, 'lxml')
        self.url = url

    def parse(self) -> Dict[str, Any]:
        # Inicjalizacja słownika z pustymi wartościami bazowymi
        data = {
            "url": self.url, "title": None, "authors": [], "category": None,
            "avg_rating": None, "rating_count": None, "cover_url": None,
            "description": None, "tags": [], "publisher": None,
            "narrators": [], "release_date": None, "release_year": None,
            "length_str": None, "duration_minutes": None, "language": None,
            "reviews": []
        }
        
        # 1. Tytuł (z ostatecznym fallbackiem na meta tagi)
        title_el = self.soup.select_one('h1[data-ta="title"]')
        if title_el:
            data['title'] = re.sub(r'(?i)\s*-\s*audiobook\s*', '', title_el.text).strip()
        else:
            meta_title = self.soup.select_one('meta[property="og:title"]')
            if meta_title and meta_title.get('content'):
                data['title'] = meta_title['content'].replace(' - audiobook', '').strip()

        # 2. Autorzy (Zamknięte w głównej sekcji)
        main_info = self.soup.select_one('[data-ta-section="ProductMainInfoDesktop"]')
        if main_info:
            author_els = main_info.select('[data-ta="smartauthor"]')
            for el in author_els:
                author_name = el.get('title')
                if author_name:
                    data['authors'].append(author_name.strip())
                else:
                    a_tag = el.select_one('a')
                    if a_tag and a_tag.text:
                        data['authors'].append(a_tag.text.strip())
                    elif el.text:
                        data['authors'].append(el.text.strip())

        # 3. Kategorie
        cat_els = self.soup.select('a[data-ta="breadcrumb-category"]')
        data['categories'] = list(dict.fromkeys(
            label for el in cat_els if (label := el.get_text(" ", strip=True))
            and label not in {"Audiobooki", "Audiobooki i Ebooki", "Strona główna", "Empik"}
        ))
        data['category'] = data['categories'][-1] if data['categories'] else None

        # 4. Ocena
        score_el = self.soup.select_one('span[data-ta="rating-count"]')
        if score_el:
            try:
                data['avg_rating'] = float(score_el.text.replace(',', '.').strip())
            except ValueError:
                pass

        count_link = self.soup.select_one('a[class*="ratingCount"]')
        if count_link:
            clean_count = re.sub(r'\D', '', count_link.text)
            if clean_count:
                data['rating_count'] = int(clean_count)

        if data['avg_rating'] is None:
            for script in self.soup.find_all('script', type='application/ld+json'):
                try:
                    json_data = json.loads(script.string)
                    if isinstance(json_data, dict) and json_data.get('@type') == 'Product':
                        agg_rating = json_data.get('aggregateRating')
                        if agg_rating:
                            data['avg_rating'] = float(agg_rating.get('ratingValue', 0))
                            data['rating_count'] = int(agg_rating.get('reviewCount', 0))
                            break
                except Exception:
                    continue

        # 5. Okładka (z fallbackiem na tagi Open Graph)
        cover_el = self.soup.select_one('picture[data-ta="cover"] img')
        if cover_el and cover_el.get('src'):
            raw_url = cover_el['src']
            data['cover_url'] = re.sub(r'/cdn-cgi/image/[^/]+,', '', raw_url)
        else:
            meta_img = self.soup.select_one('meta[property="og:image"]')
            if meta_img and meta_img.get('content'):
                data['cover_url'] = meta_img['content']

        # 6. Opis (Expanded -> Folded -> Section -> Meta Tag Fallback)
        desc_expanded = self.soup.select_one('div[data-ta="description-expanded"]')
        desc_folded = self.soup.select_one('div[data-ta="description-folded-up"]')
        target_desc = desc_expanded if desc_expanded else desc_folded

        if target_desc:
            data['description'] = target_desc.get_text(separator='\n', strip=True)
        else:
            desc_fallback = self.soup.select_one('section[data-ta-section="ProductDescription"]')
            if desc_fallback:
                paragraphs = desc_fallback.find_all('p')
                if paragraphs:
                    data['description'] = '\n'.join([p.get_text(strip=True) for p in paragraphs])
            
            # Ostateczny ratunek dla opisu
            if not data['description']:
                meta_desc = self.soup.select_one('meta[property="og:description"]')
                if meta_desc and meta_desc.get('content'):
                    data['description'] = meta_desc['content'].strip()

        # 7. Tagi
        tag_els = self.soup.select('span[class*="productTag"]')
        data['tags'] = [t.text.strip() for t in tag_els if t.text.strip() and t.text.strip() != '...']

        # 8. Szczegóły (Podwójne ssanie: Główna Tabela + Skrócona Górna Tabela)
        
        # Opcja A: Główna, długa tabela na dole strony
        for row in self.soup.select('tr[data-ta="row"]'):
            th = row.select_one('th[data-ta="attribute-name"]')
            td = row.select_one('td[data-ta="attribute-value"]')
            if th and td:
                self._apply_detail(data, th.get_text(strip=True), td.get_text(strip=True))
                
        # Opcja B (Fallback): Skrócona tabela pod ceną (dla brakujących danych)
        for wrapper in self.soup.select('div[class*="labelWrapper"]'):
            lbl = wrapper.select_one('div[class*="label"]')
            val = wrapper.select_one('div[data-ta="attribute-value"]')
            if lbl and val:
                self._apply_detail(data, lbl.get_text(strip=True), val.get_text(strip=True))

        # Zabezpieczenie: Oczyszczamy autorów z ewentualnych duplikatów po tabelach
        data['authors'] = list(dict.fromkeys(data['authors']))

        # 9. Recenzje
        review_els = self.soup.select('div[data-ta="review"]')
        for rel in review_els:
            author = rel.select_one('[data-ta="review-author-name"]')
            rating_div = rel.select_one('[data-ta-rating]')
            date = rel.select_one('[data-ta="review-date-of-add"]')
            text = rel.select_one('[data-ta="review-text"]')
            
            data['reviews'].append({
                "username": author.text.strip() if author else "Anonim",
                "rating": int(float(rating_div['data-ta-rating'])) if rating_div else None,
                "date": date.text.strip() if date else None,
                "text": text.text.strip() if text else ""
            })

        return data

    # --- Funkcje Pomocnicze ---

    def _apply_detail(self, data: Dict[str, Any], key_text: str, val_text: str):
        """Fill missing fields without overwriting previously parsed metadata."""
        key = key_text.lower()
        val = val_text.replace('\xa0', ' ') 
        
        if 'wydawnictwo' in key and not data['publisher']:
            data['publisher'] = val
        elif 'autor' in key and not data['authors']:
            data['authors'] = [a.strip() for a in val.split(',')]
        elif 'lektor' in key and not data['narrators']:
            data['narrators'] = [n.strip() for n in val.split(',')]
        elif 'data premiery' in key and not data['release_date']:
            data['release_date'] = val
            year_match = re.search(r'\d{4}', val)
            if year_match:
                data['release_year'] = int(year_match.group(0))
        elif 'czas trwania' in key and not data['length_str']:
            data['length_str'] = val
            data['duration_minutes'] = self._parse_duration(val)
        elif 'język' in key and not data['language']:
            data['language'] = val

    def _parse_duration(self, time_str: str) -> int:
        h = re.search(r'(\d+)\s*godz', time_str)
        m = re.search(r'(\d+)\s*min', time_str)
        hours = int(h.group(1)) if h else 0
        minutes = int(m.group(1)) if m else 0
        return (hours * 60) + minutes