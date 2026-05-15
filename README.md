# Lead Auto-Fill

Petit module Odoo pour aider à compléter automatiquement une piste CRM.

L'idée est simple: quand une fiche prospect est incomplète, le module essaie de retrouver quelques informations utiles sur le web, puis propose de remplir les champs manquants à votre place. On garde donc un vrai gain de temps, sans perdre le contrôle sur ce qui est écrit dans la fiche.

Il ajoute un bouton **Auto-fill** sur la fiche prospect. Quand on le lance, le module cherche des infos utiles sur l’entreprise ou le contact, puis essaie de remplir les champs manquants à partir de ces données.
Quand seul le nom du contact est présent, il tente aussi d’identifier l’entreprise associée, puis réutilise ce nom d’entreprise pour enrichir la fiche société.

## Fonctions principales

Le coeur du module est dans [crm_lead.py](models/crm_lead.py). Il est découpé en petites fonctions qui suivent le même flux:

- `action_auto_fill()` lance tout le processus depuis le bouton **Auto-fill**;
- `_plan_searches()` demande à Groq de préparer des requêtes de recherche ciblées;
- `_parallel_search()` exécute les recherches web en parallèle avec Tavily;
- `_extract_fields()` relit les résultats et essaie d'en sortir les bonnes valeurs;
- `_apply_extracted_data()` applique les données trouvées sur la fiche CRM;
- `_notify()` renvoie un message clair à l'utilisateur quand il manque une clé API, qu'aucune donnée n'est trouvée, ou quand la mise à jour est terminée.

Il y a aussi quelques fonctions d'aide pour garder le code propre:

- `_get_company_missing_fields()` et `_get_contact_missing_fields()` listent ce qui manque encore;
- `_company_field_desc()` et `_contact_field_desc()` décrivent les champs à enrichir;
- `_groq_call()` et `_tavily_search()` encapsulent les appels aux API externes;
- `_strip_markdown()`, `_get_param()`, `_email_matches_company_name()` et `_resolve_email_value()` servent au nettoyage et aux vérifications.

En pratique, le module suit donc une logique simple: il regarde la fiche, prépare des recherches, récupère les infos utiles, puis remplit uniquement ce qui manque encore.

## Configuration

Dans les paramètres Odoo, renseignez les clés API suivantes :

- **Groq API Key**
- **Tavily API Key**

## Utilisation

1. Ouvrez une fiche prospect.
2. Cliquez sur **Auto-fill**.
3. Vérifiez les champs proposés avant de sauvegarder.

## Remarque

Le but n'est pas de tout remplir automatiquement à n'importe quel prix. Le module aide surtout à démarrer plus vite, puis l'utilisateur garde la main pour valider ou corriger les données.

## Dépendances

- `crm`
- `base_setup`