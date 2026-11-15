"""Verify product ownership before downloaded data can reach any catalogue database."""

import re
import unicodedata
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Comment

from urls import canonical, identity, audioteka_product_redirect


def title_key(value):
    """Compare titles exactly after whitespace, Unicode and explicit format suffix normalization."""
    value = re.sub(r"\s*[-–—]\s*(?:audiobooki?|ebooki?)\s*$", "", str(value or ""), flags=re.I)
    return unicodedata.normalize("NFC", " ".join(value.split())).casefold()


def validate_download(site, html, response_url, requested_url, data):
    """Reject missing/conflicting identity evidence and mismatched visible product titles."""
    from parse_books import BookValidationError, jsonld, structured_product
    from source_parsers import json_roots, objects

    final_key = identity(site, response_url)
    requested_key = identity(site, requested_url)
    result_key = identity(site, data.get("url", ""))
    if not final_key or not requested_key or final_key != result_key:
        raise BookValidationError(f"Niezgodny identyfikator wyniku: {requested_url}")
    aliases = {str(value) for value in data.get("source_ids", [])} if site == "legimi" else set()
    if requested_key != final_key and requested_key not in aliases:
        if site != "audioteka" or not audioteka_product_redirect(requested_url, response_url):
            raise BookValidationError(f"Przekierowanie do innego produktu: {requested_url}")
        if re.fullmatch(r"[0-9a-f-]{36}", urlsplit(requested_url).path.strip("/").split("/")[-1], re.I):
            reference = str((data.get("source_metadata") or {}).get("reference_id", ""))
            if reference.casefold() != urlsplit(requested_url).path.strip("/").split("/")[-1].casefold():
                raise BookValidationError(f"Niepotwierdzony alias UUID Audioteki: {requested_url}")
    if site == "wolnelektury":
        evidence = ["book_api_url"]
    else:
        soup = BeautifulSoup(html, "lxml")
        evidence = []
        own = structured_product(soup, site, response_url)
        for selector, attribute, label in (('link[rel="canonical"][href]', "href", "canonical"),
                                            ('meta[property="og:url"][content]', "content", "og_url")):
            for node in soup.select(selector):
                key = identity(site, urljoin(response_url, node[attribute]))
                if key == final_key:
                    evidence.append(label)
                elif site == "lubimyczytac" and label == "canonical":
                    og = soup.select_one('meta[property="og:url"][content]')
                    books = [obj for obj in jsonld(soup) if isinstance(obj, dict) and obj.get("@type") in ("Book", "Audiobook")]
                    if not (og and identity(site, og["content"]) == final_key and
                            any(identity(site, obj.get("url", "")) == final_key for obj in books)):
                        raise BookValidationError(f"Niezgodny canonical: {requested_url}")
                else:
                    raise BookValidationError(f"Niezgodne potwierdzenie {label}: {requested_url}")
        target = own.get("url") or own.get("@id")
        if isinstance(target, dict):
            target = target.get("@id")
        if isinstance(target, str) and (identity(site, urljoin(response_url, target)) == final_key or
                                       site == "bookbeat" and target == final_key):
            evidence.append("product_jsonld")
        if site == "bookbeat" and any(str(obj.get("bookid", obj.get("id"))) == final_key and "audiobooklength" in obj
                                      for root in json_roots(soup) for obj in objects(root)):
            evidence.append("product_state")
        if site == "legimi" and final_key in aliases:
            evidence.append("product_state")
        if site == "virtualo":
            for widget in soup.select("#products_top_view[data-product-id]"):
                if widget["data-product-id"] != final_key:
                    raise BookValidationError("Virtualo: kontekst listy produktów dotyczy innego tytułu")
                evidence.append("product_widget_context")
        if not evidence:
            raise BookValidationError(f"Brak potwierdzenia ID produktu w treści strony: {requested_url}")
        heading = soup.select_one({"empik": 'h1[data-ta="title"]', "lubimyczytac": "h1.book__title",
                                   "woblink": "h1#book-title"}.get(site, "h1"))
        if heading:
            direct = " ".join(part.strip() for part in heading.find_all(string=True, recursive=False)
                              if not isinstance(part, Comment) and part.strip())
            visible = {title_key(direct), title_key(heading.get_text(" ", strip=True))}
            if not title_key(data.get("title")) or title_key(data["title"]) not in visible:
                raise BookValidationError(f"Tytuł danych nie odpowiada nagłówkowi produktu: {requested_url}")
            if own.get("name") and title_key(own["name"]) not in visible:
                raise BookValidationError(f"Tytuł JSON-LD nie odpowiada produktowi: {requested_url}")
        elif (site == "nexto" and data.get("source_archive") and "product_jsonld" in evidence
              and own.get("name") and title_key(own["name"]) == title_key(data.get("title"))):
            evidence.append("archived_product_jsonld")
        else:
            raise BookValidationError(f"Brak nagłówka właściwego produktu: {requested_url}")
    if requested_key != final_key:
        evidence.append("verified_alias")
    data["source_identity"] = {"site": site, "product_key": final_key, "requested_key": requested_key,
                               "requested_url": canonical(site, requested_url), "evidence": sorted(set(evidence))}
    validate_record(site, data)
    return data


def validate_record(site, data, row=None):
    """Prevent any payload or fill operation from crossing a stable product identity."""
    from parse_books import BookValidationError

    key = identity(site, data.get("url", ""))
    if not key:
        raise BookValidationError("Brak poprawnego identyfikatora zapisywanych danych")
    proof = data.get("source_identity")
    if proof is not None and (not isinstance(proof, dict) or proof.get("site") != site or proof.get("product_key") != key):
        raise BookValidationError("Potwierdzenie źródła dotyczy innego produktu")
    if site == "lubimyczytac" and data.get("external_id") is not None and str(data["external_id"]) != key:
        raise BookValidationError("external_id LC nie odpowiada adresowi książki")
    if row is not None:
        stored_key = str(row["external_id"]) if site == "lubimyczytac" else identity(site, row["url"])
        aliases = {str(value) for value in data.get("source_ids", [])} if site == "legimi" else set()
        verified_alias = (site == "audioteka" and isinstance(proof, dict)
                          and proof.get("requested_key") == stored_key and "verified_alias" in proof.get("evidence", []))
        if stored_key != key and stored_key not in aliases and not verified_alias:
            raise BookValidationError("Próba przypisania danych do innego rekordu książki")
        if (proof is not None and row.get("title") and data.get("title")
                and title_key(row["title"]) != title_key(data["title"])):
            raise BookValidationError("Tytuł pobrany z serwisu różni się od tytułu w bazie; wymagane sprawdzenie")
    return key
