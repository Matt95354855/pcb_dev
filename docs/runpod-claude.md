# Qwen auto-hébergé sur Runpod et Claude par API

Configuration indiquée par l'utilisateur : Qwen « 3.8 27B » sur A40, avec
possibilité d'étendre les ressources Runpod vers H100 et davantage de RAM.
L'identifiant exact servi, la quantification, le serveur et son endpoint restent
à confirmer. Aucune performance ou compatibilité d'appels d'outils n'est encore
mesurée par ce projet.

## Qwen et le connecteur existant

Un serveur vLLM sur Runpod peut exposer une API compatible Chat Completions par
un endpoint HTTPS. Le serveur doit être authentifié avant toute exposition
publique ; le proxy Runpod ne remplace pas l'authentification de l'inférence.
Voir les documentations officielles [vLLM/Runpod](https://docs.vllm.ai/en/latest/deployment/frameworks/runpod/)
et [déploiement Runpod](https://www.runpod.io/articles/guides/deploy-vllm-runpod-docker).

Le fichier [examples/runpod-model.json](../examples/runpod-model.json) est un
**gabarit**, pas une connexion prête à utiliser. Remplacer l'URL et l'identifiant
par ceux réellement servis, puis adapter les capacités et limites au serveur.
La clé reste dans l'environnement sous `PCB_QWEN_KEY`, hors du dépôt et des prompts.

Si l'application tourne hors du Pod, le transport effectue une connexion réseau :
`offline=false`, `allow_network=true` et l'option CLI `--allow-network` sont
nécessaires. Auto-hébergé ne signifie pas loopback. Si l'application et le serveur
tournent ensemble sur la même machine, une configuration loopback distincte peut
conserver le mode hors ligne du client.

Après configuration explicite, le self-test de protocole peut vérifier un premier
appel d'outil. Il ne prouve pas la compétence de conception du modèle. Le parcours
actuel `agent-run` exige encore un design préexistant ; le démarrage depuis un
prompt seul est la prochaine étape décrite dans [la vision produit](prompt-first.md).

## Claude et répartition des rôles

L'utilisateur envisage une clé Claude API. Le rôle reste à choisir : Qwen manager
avec revue Claude ponctuelle, ou Claude manager avec tâches déléguées à Qwen.
Aucun basculement automatique ni envoi de projet à Claude n'est configuré.

Le connecteur actuel ne parle pas nativement l'API Messages d'Anthropic. Son
adaptateur devra convertir les messages, `tool_use` et `tool_result` vers le
contrat interne, préserver les budgets et valider les arguments. Une clé Claude
ne doit pas être injectée directement dans le gabarit Qwen.
Référence : [API Messages et outils Claude](https://platform.claude.com/docs/en/api/messages/create).

La répartition retenue devra conserver les mêmes outils et critères de preuve,
avec une politique explicite de routage, un budget par fournisseur et la
traçabilité des décisions. Ajouter un fournisseur n'améliore pas automatiquement
les poids de Qwen : l'évaluation, la mémoire et un éventuel entraînement restent
des mécanismes distincts.

Ces fichiers ne provisionnent aucun GPU et n'effectuent aucun appel d'inférence.
