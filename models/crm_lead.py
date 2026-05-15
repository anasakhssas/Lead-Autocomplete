# -*- coding: utf-8 -*-
import json
import logging
import re
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

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

    # Button.
    def action_auto_fill(self):
        self.ensure_one()
        _logger.info("Auto-fill started for lead %s", self.id)

        groq_key   = self._get_param("lead_autofill.groq_api_key")
        tavily_key = self._get_param("lead_autofill.tavily_api_key")

        if not groq_key:
            raise UserError(_("Groq API key is not configured."))
        if not tavily_key:
            raise UserError(_("Tavily API key is not configured."))

        company_name  = self.partner_name or (self.partner_id.name if self.partner_id else "")
        contact_name  = self.contact_name or ""
        contact_email_hint = self.email_from or self.email_cc or ""
        city_hint     = self.city or (self.partner_id.city if self.partner_id else "")

        if not company_name and not contact_name:
            raise UserError(_(
                "Cannot auto-fill: at least a company name or contact name is required."
            ))

        if not company_name and contact_name:
            inferred_company_name = self._infer_company_name_from_contact(
                contact_name=contact_name,
                contact_email_hint=contact_email_hint,
                city_hint=city_hint,
                groq_key=groq_key,
                tavily_key=tavily_key,
            )
            if inferred_company_name:
                company_name = inferred_company_name
                self._apply_extracted_data(
                    {"company_name": inferred_company_name},
                    company_name="",
                    write_phone=False,
                )
                _logger.info(
                    "Auto-fill inferred company name %s for lead %s",
                    inferred_company_name,
                    self.id,
                )

        company_fields = self._get_company_missing_fields() if company_name else []
        contact_fields = self._get_contact_missing_fields() if contact_name else []

        if not company_fields and not contact_fields:
            return self._notify("All fields are already filled.", "info")

        # ── Stage 1: generate all search queries in ONE LLM call ──────────
        queries = self._plan_searches(
            company_name  = company_name,
            contact_name  = contact_name,
            contact_email_hint = contact_email_hint,
            city_hint     = city_hint,
            company_fields = company_fields,
            contact_fields = contact_fields,
            groq_key      = groq_key,
        )
        if not queries:
            return self._notify("AI could not plan searches.", "warning")

        _logger.info("Planned %d search queries: %s", len(queries), queries)

        # ── Stage 2: run all Tavily searches in PARALLEL ──────────────────
        raw_results = self._parallel_search(queries, tavily_key)
        if not raw_results:
            return self._notify("Web search returned no results.", "warning")

        # ── Stage 3: extract company and contact data separately ─────────
        company_fields_map = {f: self._company_field_desc()[f] for f in company_fields}
        contact_fields_map = {f: self._contact_field_desc()[f] for f in contact_fields}

        extracted_company = None
        extracted_contact = None

        if company_fields_map:
            extracted_company = self._extract_fields(
                search_blob  = raw_results,
                fields       = company_fields_map,
                company_name = company_name,
                contact_name = contact_name,
                contact_email_hint = contact_email_hint,
                groq_key     = groq_key,
                source_type  = "company",
            )
            if extracted_company:
                _logger.debug("Extracted company payload: %s", extracted_company)
                self._apply_extracted_data(
                    extracted_company,
                    company_name=company_name,
                    write_phone=False,
                )

        if contact_fields_map:
            extracted_contact = self._extract_fields(
                search_blob  = raw_results,
                fields       = contact_fields_map,
                company_name = company_name,
                contact_name = contact_name,
                contact_email_hint = contact_email_hint,
                groq_key     = groq_key,
                source_type  = "contact",
            )
            if extracted_contact:
                _logger.debug("Extracted contact payload: %s", extracted_contact)
                self._apply_extracted_data(
                    extracted_contact,
                    company_name=company_name,
                    write_phone=False,
                )

        if not extracted_company and not extracted_contact:
            return self._notify("AI could not extract any data.", "warning")

        final_email_from = None
        if extracted_contact:
            contact_email = extracted_contact.get("email_from")
            if contact_email and self._email_matches_company_name(contact_email, company_name):
                final_email_from = contact_email
        if not final_email_from and extracted_company:
            company_email = extracted_company.get("email_from")
            if company_email and self._email_matches_company_name(company_email, company_name):
                final_email_from = company_email
        final_email_cc = self._resolve_email_value(
            contact_value=extracted_contact.get("email_cc") if extracted_contact else None,
            company_value=extracted_company.get("email_cc") if extracted_company else None,
            company_name=company_name,
        )

        email_vals = {}
        if final_email_from and not self.email_from:
            email_vals["email_from"] = final_email_from
        if final_email_cc and not self.email_cc:
            email_vals["email_cc"] = final_email_cc

        if email_vals:
            self.write(email_vals)
            _logger.info("Auto-fill wrote %s to lead %s", list(email_vals.keys()), self.id)

        final_phone = None
        if extracted_contact:
            final_phone = extracted_contact.get("phone")
        if not final_phone and extracted_company:
            final_phone = extracted_company.get("phone")

        if final_phone and not self.phone:
            self.write({"phone": final_phone})
            _logger.info("Auto-fill wrote phone to lead %s", self.id)

        return self._notify("Lead fields updated successfully!", "success")

    # ------------------------------------------------------------------
    # Stage 1 — Plan: ask Groq for an optimal list of search queries
    # ------------------------------------------------------------------

    # planification des requêtes de recherche à faire sur Tavily, en fonction des champs manquants et du contexte du lead.
    def _plan_searches(
        self,
        company_name: str,
        contact_name: str,
        contact_email_hint: str,
        city_hint: str,
        company_fields: list,
        contact_fields: list,
        groq_key: str,
    ) -> list[str]:
        """
        One cheap LLM call that returns a JSON array of search queries.
        Keeps queries targeted so Tavily results are high-signal.
        """
        cf_desc = {f: self._company_field_desc()[f] for f in company_fields}
        kf_desc = {f: self._contact_field_desc()[f]  for f in contact_fields}
        morocco_queries = self._morocco_company_location_queries(
            company_name=company_name,
            company_fields=company_fields,
            city_hint=city_hint,
        )

        prompt = f"""
            You are a search-query planner for a CRM enrichment tool.

            Context:
            Company : {company_name or 'N/A'}
            Contact : {contact_name or 'N/A'}
                Email   : {contact_email_hint or 'N/A'}
            City    : {city_hint    or 'N/A'}

            Missing company fields : {json.dumps(cf_desc, ensure_ascii=False)}
            Missing contact fields : {json.dumps(kf_desc, ensure_ascii=False)}

            Generate the MINIMUM number of targeted web-search queries (max 5) needed
            to find the missing fields above. Prefer queries that combine the company
            name with a specific intent (e.g. "address", "phone", "contact email").
            For company street or city searches, prioritize Morocco-first queries when
            the company may have an office there.
            For contact fields always include the company name for precision.
            If an email is available, include it in contact queries too.

            Prefer these Morocco-first company location queries when relevant:
            {json.dumps(morocco_queries, ensure_ascii=False)}

            Return ONLY a JSON array of query strings, nothing else.
            Example: ["Acme Corp Paris address phone", "John Doe Acme Corp email title"]
            """
        resp = self._groq_call(
            groq_key=groq_key,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=300,
            temperature=0.0,
        )
        if resp is None:
            return morocco_queries
        try:
            queries = json.loads(self._strip_markdown(resp))
            planned_queries = [q for q in queries if isinstance(q, str) and q.strip()][:5]
            return self._merge_queries(morocco_queries, planned_queries)
        except (json.JSONDecodeError, TypeError):
            _logger.error("Query planner returned invalid JSON: %s", resp)
            return morocco_queries

    # ------------------------------------------------------------------
    # Stage 2 — Parallel Tavily searches
    # ------------------------------------------------------------------

    # recherche parallèle de toutes les requêtes planifiées sur Tavily, avec un maximum de 5 threads pour éviter de surcharger l'API. Les résultats sont ensuite combinés en un seul blob de texte pour l'étape d'extraction.
    def _parallel_search(self, queries: list[str], tavily_key: str) -> str:
        """Run all queries concurrently; join results into one text blob."""
        results = {}

        def _search(q):
            return q, self._tavily_search(q, tavily_key, depth="basic", max_results=3)

        with ThreadPoolExecutor(max_workers=min(len(queries), 5)) as pool:
            futures = {pool.submit(_search, q): q for q in queries}
            for future in as_completed(futures):
                q, text = future.result()
                if text:
                    results[q] = text

        if not results:
            return ""

        parts = [f"### Search: {q}\n{txt}" for q, txt in results.items()]
        blob  = "\n\n---\n\n".join(parts)
        # Keep within ~6 k chars to leave room for the extraction prompt
        return blob[:6000]

    # ------------------------------------------------------------------
    # Stage 3 — Extract: one LLM call over the combined search blob
    # ------------------------------------------------------------------

    # extraction finale des champs manquants à partir du blob de résultats de recherche, en demandant à Groq de retourner un JSON structuré. Les règles d'extraction sont adaptées selon qu'on cherche des infos sur l'entreprise ou le contact, notamment pour le téléphone.
    def _extract_fields(
        self,
        search_blob: str,
        fields: dict,
        company_name: str,
        contact_name: str,
        contact_email_hint: str,
        groq_key: str,
        source_type: str,
    ) -> dict | None:
        if source_type == "company":
            phone_rule = "- For phone, return the main company phone number."
        else:
            phone_rule = (
                "- For phone, return the contact person's direct phone number. "
                "If multiple phones appear, prefer the one most clearly tied to the contact."
            )

        prompt = f"""
                You are a CRM data-extraction assistant.

                Company : {company_name or 'N/A'}
                Contact : {contact_name or 'N/A'}
                Email   : {contact_email_hint or 'N/A'}

                Below are web-search results collected for this lead.
                Extract ONLY the following fields from them:
                {json.dumps(fields, indent=2, ensure_ascii=False)}

                Rules:
                - Output a single valid JSON object with exactly these keys.
                - Set a field to null if you cannot find reliable data for it.
                - Never invent or guess values.
                - For website, return the full URL (https://...).
                {phone_rule}

                === Search Results ===
                {search_blob}
                === End of Results ===

                JSON output:
                """
        
        raw = self._groq_call(
            groq_key=groq_key,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=500,
            temperature=0.0,
        )
        if raw is None:
            return None
        try:
            return json.loads(self._strip_markdown(raw))
        except json.JSONDecodeError:
            _logger.error("Extractor returned invalid JSON: %s", raw)
            return None

    def _infer_company_name_from_contact(
        self,
        contact_name: str,
        contact_email_hint: str,
        city_hint: str,
        groq_key: str,
        tavily_key: str,
    ) -> str | None:
        if not contact_name:
            return None

        queries = self._company_inference_queries(
            contact_name=contact_name,
            contact_email_hint=contact_email_hint,
            city_hint=city_hint,
        )
        if not queries:
            return None

        raw_results = self._parallel_search(queries, tavily_key)
        if not raw_results:
            return None

        extracted = self._extract_fields(
            search_blob=raw_results,
            fields={"company_name": "current employer or company name"},
            company_name="",
            contact_name=contact_name,
            contact_email_hint=contact_email_hint,
            groq_key=groq_key,
            source_type="contact",
        )
        if not extracted:
            return None

        company_name = extracted.get("company_name")
        if isinstance(company_name, str):
            company_name = company_name.strip()
            return company_name or None
        return None

    # ------------------------------------------------------------------
    # Shared Groq helper
    # ------------------------------------------------------------------

    def _groq_call(
        self,
        groq_key: str,
        messages: list,
        max_tokens: int = 500,
        temperature: float = 0.0,
    ) -> str | None:
        """Single Groq completion; returns the text content or None on error."""
        try:
            resp = requests.post(
                GROQ_API_URL,
                headers={
                    "Authorization": f"Bearer {groq_key}",
                    "Content-Type":  "application/json",
                },
                json={
                    "model":       GROQ_MODEL,
                    "messages":    messages,
                    "temperature": temperature,
                    "max_tokens":  max_tokens,
                },
                timeout=30,
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"].strip()
        except (requests.RequestException, KeyError, ValueError) as exc:
            _logger.error("Groq call failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Tavily search (unchanged)
    # ------------------------------------------------------------------

    def _tavily_search(
        self,
        query: str,
        api_key: str,
        depth: str = "basic",
        max_results: int = 3,
        include_domains: list | None = None,
    ) -> str:
        payload = {
            "api_key":             api_key,
            "query":               query,
            "search_depth":        depth,
            "include_answer":      True,
            "include_raw_content": False,
            "max_results":         max_results,
        }
        if include_domains:
            payload["include_domains"] = include_domains
        try:
            resp = requests.post(TAVILY_API_URL, json=payload, timeout=20)
            resp.raise_for_status()
        except requests.RequestException as exc:
            _logger.warning("Tavily search failed for '%s': %s", query, exc)
            return ""
        try:
            data = resp.json()
        except ValueError:
            return ""

        parts = []
        answer = (data.get("answer") or "").strip()
        if answer:
            parts.append(f"[Summary]\n{answer}")
        for item in data.get("results", []):
            title   = (item.get("title")   or "").strip()
            content = (item.get("content") or "").strip()
            url     = (item.get("url")     or "").strip()
            if content:
                header = f"[{title}]({url})" if title else f"[{url}]"
                parts.append(f"{header}\n{content}")
        return "\n\n".join(parts)

    @staticmethod
    def _company_inference_queries(
        contact_name: str,
        contact_email_hint: str = "",
        city_hint: str = "",
    ) -> list[str]:
        if not contact_name:
            return []

        queries = [
            f'"{contact_name}" company',
            f'"{contact_name}" works at',
            f'"{contact_name}" employer',
        ]
        if city_hint:
            queries.append(f'"{contact_name}" {city_hint} company')
        if contact_email_hint and "@" in contact_email_hint:
            domain = contact_email_hint.split("@", 1)[1].strip()
            if domain:
                queries.append(f'"{contact_name}" {domain}')

        deduped_queries = []
        for query in queries:
            query = query.strip()
            if query and query not in deduped_queries:
                deduped_queries.append(query)
        return deduped_queries[:5]

    @staticmethod
    def _morocco_company_location_queries(
        company_name: str,
        company_fields: list,
        city_hint: str = "",
    ) -> list[str]:
        if not company_name or not any(field in {"street", "city"} for field in company_fields):
            return []

        queries = [
            f"{company_name} Morocco address",
            f"{company_name} Morocco city",
            f"{company_name} Morocco office address",
        ]
        if city_hint:
            queries.insert(1, f"{company_name} {city_hint} Morocco address")

        deduped_queries = []
        for query in queries:
            query = query.strip()
            if query and query not in deduped_queries:
                deduped_queries.append(query)
        return deduped_queries[:5]

    @staticmethod
    def _merge_queries(priority_queries: list[str], planned_queries: list[str]) -> list[str]:
        merged = []
        for query in priority_queries + planned_queries:
            if query and query not in merged:
                merged.append(query)
        return merged[:5]

    # ------------------------------------------------------------------
    # Field helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _company_field_desc() -> dict:
        return {
            "street":  "street address line 1 (e.g. '10 Rue de la Paix')",
            "zip":     "postal / ZIP code",
            "city":    "city name",
            "website": "official website URL (e.g. 'https://example.com')",
            "email_from": "main company email address",
            "email_cc":   "secondary company email address",
            "phone":   "main company phone number",
        }

    @staticmethod
    def _contact_field_desc() -> dict:
        return {
            "email_from": "contact's main email address",
            "email_cc":   "contact's secondary/cc email address",
            "function":   "contact's job title or position",
            "phone":      "contact's direct phone number",
        }

    # Separation company et contact. Si pas de nom de contact, on considère que c'est un lead "company-only" et on ne demande pas les champs contact. Inversement, s'il y a un nom de contact, on considère que c'est un lead "contact-related" et on demande les champs contact (même si certains sont vides) car ils sont souvent plus facilement trouvables à partir du nom du contact que de celui de l'entreprise. Par exemple, même si l'email du contact est vide dans Odoo, il est souvent mentionné dans les résultats de recherche à côté du nom du contact, ce qui permet à l'extracteur de le récupérer.
    def _get_company_missing_fields(self) -> list:
        return [f for f, v in {
            "street": self.street, "zip": self.zip, "city": self.city,
            "website": self.website, "phone": self.phone,
            "email_from": self.email_from,
        }.items() if not v]

    def _get_contact_missing_fields(self) -> list:
        if not self.contact_name:
            return []
        return [f for f, v in {
            "email_from": self.email_from, "email_cc": self.email_cc,
            "function": self.function, "phone": self.phone,
        }.items() if not v]

    # ------------------------------------------------------------------
    # Apply results
    # ------------------------------------------------------------------

    def _apply_extracted_data(self, data: dict, company_name: str = "", write_phone: bool = True):
        mapping = {
            "company_name": "partner_name",
            "street": "street", "street2": "street2", "zip": "zip",
            "city": "city", "website": "website",
            "function": "function",
        }
        vals = {}
        for json_key, field_name in mapping.items():
            value = data.get(json_key)
            if not value:
                continue
            if json_key in {"email_from", "email_cc"}:
                if not self._email_matches_company_name(value, company_name):
                    _logger.info(
                        "Auto-fill skipped %s (domain mismatch) for lead %s",
                        json_key, self.id,
                    )
                    continue
            if not getattr(self, field_name):
                vals[field_name] = value

        if write_phone:
            phone_value = data.get("phone")
            if phone_value and not self.phone:
                vals["phone"] = phone_value

        if vals:
            self.write(vals)
            _logger.info("Auto-fill wrote %s to lead %s", list(vals.keys()), self.id)
        else:
            _logger.info("Auto-fill: no new values for lead %s", self.id)

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    # Garder uniquement le JSON brut.
    @staticmethod
    def _strip_markdown(text: str) -> str:
        text = text.strip()
        if text.startswith("```"):
            parts = text.split("```")
            text = parts[1] if len(parts) > 1 else text
            if text.startswith("json"):
                text = text[4:]
        return text.strip()
    
    # Récupère valeur dans les paramètres.
    def _get_param(self, key: str) -> str:
        return self.env["ir.config_parameter"].sudo().get_param(key)

    # Fait une vérification heuristique entre l’email et le nom de la société.
    def _email_matches_company_name(self, email: str, company_name: str) -> bool:
        if not email or not company_name:
            return False
        _, _, domain = email.lower().strip().partition("@")
        if not domain:
            return False
        tokens = re.findall(r"[a-z0-9]+", company_name.lower())
        return any(t and t in domain for t in tokens)

    # choisit la meilleure valeur entre l’e-mail du contact et celui de l’entreprise.
    def _resolve_email_value(self, contact_value: str | None, company_value: str | None, company_name: str) -> str | None:
        if contact_value and self._email_matches_company_name(contact_value, company_name):
            return contact_value
        if company_value and self._email_matches_company_name(company_value, company_name):
            return company_value
        return None

    # Construit une action Odoo de type notification.
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