# models/res_config_settings.py
from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    lead_autofill_groq_api_key = fields.Char(
        string="Groq API Key",
        config_parameter="lead_autofill.groq_api_key",
    )

    lead_autofill_tavily_api_key = fields.Char(
        string="Tavily API Key",
        config_parameter="lead_autofill.tavily_api_key",
    )