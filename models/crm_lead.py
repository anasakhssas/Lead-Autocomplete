# -*- coding: utf-8 -*-
import json
import logging
import requests

from odoo import models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

GROQ_API_URL   = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL     = "llama-3.3-70b-versatile"
TAVILY_API_URL = "https://api.tavily.com/search"


class CrmLead(models.Model):
    _inherit = "crm.lead"

    # ------------------------------------------------------------------
    # Public action (button)
    # ------------------------------------------------------------------

    def action_auto_fill(self):
        self.ensure_one()
        _logger.info("Auto-fill started for lead %s", self.id)

        groq_key   = self._get_param("lead_autofill.groq_api_key")
        tavily_key = self._get_param("lead_autofill.tavily_api_key")

        if not groq_key:
            raise UserError(_(
                "Groq API key is not configured. "
            ))
        if not tavily_key:
            raise UserError(_(
                "Tavily API key is not configured. "
            ))

        context = self._build_search_context()
        if not context.get("company_name") and not context.get("contact_name"):
            raise UserError(_(
                "Cannot auto-fill: at least a company name or contact name is required."
            ))

        fields_to_fill = self._get_missing_fields()
        if not fields_to_fill:
            return self._notify("All fillable fields are already filled!", "warning")

        _logger.debug("Fields to fill: %s", fields_to_fill)

        # ── Gather rich content via Tavily ─────────────────────────────

        content_parts = []

        # QUERY 1 — General contact / address info
        main_query = self._build_query(context, intent="address")
        _logger.info("Tavily main query: %s", main_query)
        result = self._tavily_search(main_query, tavily_key, depth="advanced")
        if result:
            content_parts.append(result)

        # QUERY 2 — Dedicated phone search (only if phone is missing)
        if "phone" in fields_to_fill:
            phone_query = self._build_query(context, intent="phone")
            _logger.info("Tavily phone query: %s", phone_query)
            result = self._tavily_search(phone_query, tavily_key, depth="basic")
            if result:
                content_parts.append(result)

        # QUERY 3 — Official website discovery (only if website is missing)
        if "website" in fields_to_fill and context.get("company_name"):
            site_query = self._build_query(context, intent="website")
            _logger.info("Tavily website query: %s", site_query)
            result = self._tavily_search(
                site_query, tavily_key, depth="basic",
                include_domains=[],      # no restriction — let Tavily find the homepage
                max_results=3,
            )
            if result:
                content_parts.append(result)

        if not content_parts:
            return self._notify("Could not retrieve any information from Tavily.", "warning")

        combined = "\n\n".join(content_parts)
        _logger.debug("Total Tavily content: %d chars", len(combined))

        # ── AI extraction ──────────────────────────────────────────────
        extracted = self._extract_with_groq(combined, context, fields_to_fill, groq_key)
        if not extracted:
            return self._notify("AI could not extract any data.", "warning")

        _logger.debug("Extracted payload: %s", extracted)
        self._apply_extracted_data(extracted)
        return self._notify("Lead fields updated successfully!", "success")

    # ------------------------------------------------------------------
    # Tavily search
    # ------------------------------------------------------------------

    def _tavily_search(
        self,
        query: str,
        api_key: str,
        depth: str = "advanced",
        max_results: int = 5,
        include_domains: list | None = None,
    ) -> str:
        """
        Call the Tavily Search API and return a single clean text block
        ready to feed to Groq.

        Tavily already extracts clean text from each page — no HTML parsing
        needed.  'advanced' depth triggers full-page crawling; 'basic' is
        faster and cheaper (counts as 1 API credit vs 2 for advanced).

        Returns an empty string on failure (non-fatal — we just skip this source).
        """
        payload = {
            "api_key":        api_key,
            "query":          query,
            "search_depth":   depth,          # "basic" | "advanced"
            "include_answer": True,           # Tavily's own AI-synthesised summary
            "include_raw_content": False,     # clean content is sufficient
            "max_results":    max_results,
        }
        if include_domains is not None:
            payload["include_domains"] = include_domains

        try:
            resp = requests.post(TAVILY_API_URL, json=payload, timeout=20)
            resp.raise_for_status()
        except requests.RequestException as exc:
            _logger.warning("Tavily search failed for query '%s': %s", query, exc)
            return ""

        try:
            data = resp.json()
        except ValueError:
            _logger.warning("Tavily returned invalid JSON")
            return ""

        parts = []

        # 1. Tavily's own AI-generated answer (very concise, high signal)
        answer = (data.get("answer") or "").strip()
        if answer:
            parts.append(f"[Tavily summary]\n{answer}")

        # 2. Individual result snippets with their source URL
        for item in data.get("results", []):
            title   = (item.get("title")   or "").strip()
            content = (item.get("content") or "").strip()
            url     = (item.get("url")     or "").strip()
            if content:
                header = f"[{title}]({url})" if title else f"[{url}]"
                parts.append(f"{header}\n{content}")

        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # Context & field helpers
    # ------------------------------------------------------------------

    def _build_search_context(self) -> dict:
        partner = self.partner_id
        return {
            "company_name": self.partner_name or (partner.name if partner else ""),
            "contact_name": self.contact_name or "",
            "city":    self.city or (partner.city if partner else ""),
            "country": (self.country_id.name if self.country_id else "") or
                       (partner.country_id.name if partner and partner.country_id else ""),
            "website": self.website or (partner.website if partner else ""),
            "email":   self.email_from or "",
            "phone":   self.phone or "",
        }

    def _get_missing_fields(self) -> list:
        candidates = {
            "street":  self.street,
            "street2": self.street2,
            "zip":     self.zip,
            "city":    self.city,
            "phone":   self.phone,
            "website": self.website,
        }
        return [f for f, v in candidates.items() if not v]

    def _build_query(self, context: dict, intent: str = "address") -> str:
        """
        Build a targeted Tavily query.

        intent = 'address' → full contact details (street, zip, city)
        intent = 'phone'   → phone number
        intent = 'website' → official website URL
        """
        company = context.get("company_name", "")
        city    = context.get("city", "")
        country = context.get("country", "")

        base = f'"{company}"' if company else ""
        if city:
            base += f" {city}"
        if country:
            base += f" {country}"

        suffix = {
            "address": "headquarters address contact",
            "phone":   "phone number",
            "website": "official website",
        }.get(intent, "contact")

        return f"{base} {suffix}".strip()

    # ------------------------------------------------------------------
    # Groq AI extraction
    # ------------------------------------------------------------------

    def _extract_with_groq(
        self,
        content: str,
        context: dict,
        fields_to_fill: list,
        api_key: str,
    ) -> dict | None:
        field_descriptions = {
            "street":  "street address line 1 (e.g. '10 Rue de la Paix')",
            "street2": "address complement (suite / floor / building)",
            "zip":     "postal / ZIP code",
            "city":    "city name",
            "phone":   "main phone number — prefer international format",
            "website": "official website URL (e.g. 'https://example.com')",
        }
        requested = {f: field_descriptions[f] for f in fields_to_fill if f in field_descriptions}

        system_prompt = (
            "You are a precise data-extraction assistant. "
            "Extract ONLY the requested fields from the web content provided. "
            "Reply with a SINGLE valid JSON object, nothing else. "
            "Set a field to null if you cannot find it with high confidence. "
            "Never invent or guess data."
        )

        user_prompt = f"""
=== Company context ===
Name    : {context['company_name']  or 'unknown'}
Contact : {context['contact_name'] or 'unknown'}
City    : {context['city']          or 'unknown'}
Country : {context['country']       or 'unknown'}
Website : {context['website']       or 'unknown'}
Email   : {context['email']         or 'unknown'}

=== Web content (from Tavily — already cleaned and extracted) ===
{content[:7_000]}

=== Task ===
From the content above, extract exactly these fields.
Return ONLY a valid JSON object with these keys:
{json.dumps(requested, indent=2, ensure_ascii=False)}

Example:
{{
  "street": "10 Rue de la Paix",
  "zip": "75001",
  "city": "Paris",
  "phone": "+33 1 42 00 00 00",
  "website": "https://example.fr"
}}
"""

        try:
            resp = requests.post(
                GROQ_API_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type":  "application/json",
                },
                json={
                    "model":    GROQ_MODEL,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user",   "content": user_prompt},
                    ],
                    "temperature": 0.0,
                    "max_tokens":  400,
                },
                timeout=25,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            _logger.error("Groq API call failed: %s", exc)
            return None

        raw = resp.json()["choices"][0]["message"]["content"].strip()

        # Strip markdown code fences if the model wraps its output
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]

        try:
            return json.loads(raw.strip())
        except json.JSONDecodeError:
            _logger.error("Could not parse Groq response as JSON:\n%s", raw)
            return None

    # ------------------------------------------------------------------
    # Apply results
    # ------------------------------------------------------------------

    def _apply_extracted_data(self, data: dict):
        """Write extracted values — never overwrite fields that already have data."""
        mapping = {
            "street": "street", "street2": "street2",
            "zip":    "zip",    "city":    "city",
            "phone":  "phone",  "website": "website",
        }
        vals = {}
        for json_key, field_name in mapping.items():
            value = data.get(json_key)
            if value and not getattr(self, field_name):
                vals[field_name] = value

        if vals:
            self.write(vals)
            _logger.info("Auto-fill wrote %s to lead %s", list(vals.keys()), self.id)
        else:
            _logger.info("Auto-fill: no new values to write for lead %s", self.id)

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def _get_param(self, key: str) -> str:
        return self.env["ir.config_parameter"].sudo().get_param(key)

    def _notify(self, message: str, notif_type: str = "info") -> dict:
        return {
            "type": "ir.actions.client",
            "tag":  "display_notification",
            "params": {
                "title":   _("Auto-Fill"),
                "message": _(message),
                "type":    notif_type,
                "sticky":  False,
            },
        }