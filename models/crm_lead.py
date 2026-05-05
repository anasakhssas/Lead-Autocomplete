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

        # ── AI Agentic Extraction (Groq + Tavily) ────────────────────────
        extracted = self._agentic_extractor(context, fields_to_fill, groq_key, tavily_key)
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
        Call the Tavily Search API and return a single clean text block.
        """
        payload = {
            "api_key":        api_key,
            "query":          query,
            "search_depth":   depth,
            "include_answer": True,
            "include_raw_content": False,
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
        answer = (data.get("answer") or "").strip()
        if answer:
            parts.append(f"[Tavily summary]\n{answer}")

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

    # ------------------------------------------------------------------
    # Groq AI Agentic Extraction
    # ------------------------------------------------------------------

    def _agentic_extractor(
        self,
        context: dict,
        fields_to_fill: list,
        groq_api_key: str,
        tavily_api_key: str,
    ) -> dict | None:
        """
        Agentic loop: Groq determines which searches to perform using Tavily,
        processes the retrieved text, and returns the final JSON.
        """
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
            "You are an autonomous web-search agent. Your task is to find missing information "
            "for a company based on the provided context.\n"
            "You have access to a 'web_search' tool. Use it to search for the missing fields.\n"
            "IMPORTANT: Once you have found the requested fields or determined they cannot be found, "
            "your FINAL response must be ONLY a valid JSON object matching the requested fields precisely.\n"
            "Set a field to null if you cannot find it. Never invent data."
        )

        user_prompt = f"""
=== Company Context ===
Name    : {context['company_name']  or 'unknown'}
Contact : {context['contact_name'] or 'unknown'}
City    : {context['city']          or 'unknown'}
Country : {context['country']       or 'unknown'}
Website : {context['website']       or 'unknown'}
Email   : {context['email']         or 'unknown'}

=== Missing Fields to Find ===
{json.dumps(requested, indent=2, ensure_ascii=False)}

If you have enough information right now, output the final JSON. 
If not, use the `web_search` tool to gather data.
"""

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        tools = [
            {
                "type": "function",
                "function": {
                    "name": "web_search",
                    "description": "Search the web for company information, addresses, phones, etc.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "The search query (e.g. 'OpenAI headquarters address')",
                            }
                        },
                        "required": ["query"],
                    },
                },
            }
        ]

        # Start the Agentic Loop (max 4 turns)
        for turn in range(4):
            _logger.info("Agentic turn %d...", turn + 1)
            try:
                resp = requests.post(
                    GROQ_API_URL,
                    headers={
                        "Authorization": f"Bearer {groq_api_key}",
                        "Content-Type":  "application/json",
                    },
                    json={
                        "model": GROQ_MODEL,
                        "messages": messages,
                        "tools": tools,
                        "tool_choice": "auto",
                        "temperature": 0.0,
                        "max_tokens": 800,
                    },
                    timeout=30,
                )
                resp.raise_for_status()
            except requests.RequestException as exc:
                _logger.error("Groq API call failed: %s", exc)
                return None

            response_data = resp.json()["choices"][0]["message"]
            messages.append(response_data)

            # Check if the model wants to call a tool
            if response_data.get("tool_calls"):
                for tool_call in response_data["tool_calls"]:
                    if tool_call["function"]["name"] == "web_search":
                        try:
                            args = json.loads(tool_call["function"]["arguments"])
                            query = args.get("query", "")
                        except json.JSONDecodeError:
                            query = ""

                        _logger.info("Groq is calling web_search with query: '%s'", query)
                        
                        search_result = self._tavily_search(query, tavily_api_key, depth="basic", max_results=3)
                        if not search_result:
                            search_result = "No results found."
                            
                        # Add tool result back to the conversation
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call["id"],
                            "name": "web_search",
                            "content": search_result[:4000] # limit length to save tokens
                        })
                
                # Continue loop so the model can process the tool results
                continue
            
            # If no tool calls, it should be the final output
            raw = response_data.get("content", "").strip()
            
            # Clean possible markdown wrap
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]

            try:
                final_json = json.loads(raw)
                return final_json
            except json.JSONDecodeError:
                _logger.error("Agent returned invalid JSON at end of logic: %s", raw)
                return None

        _logger.warning("Agent exceeded maximum turns")
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