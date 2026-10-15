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

Après configuration explicite, `pcb-dev model-check --config chemin/qwen.json`
vérifie un premier appel d'outil (ajouter `--allow-network` pour un endpoint distant).
Ce test consomme de l'inférence et ne prouve pas la compétence de conception.
Le [studio](studio.md) démarre désormais depuis un prompt sans design préalable ;
`agent-run` reste disponible pour travailler sur un design existant.

## Qwen manager, Claude sur demande et au plus 10 % des tokens

Qwen pilote le projet. Claude intervient uniquement après une action humaine
explicite pour un cas difficile, jamais en bascule automatique. Le connecteur natif
utilise l'API Messages d'Anthropic et garde les clés dans l'environnement. La revue
est textuelle et n'exécute aucun outil ; elle est fournie à Qwen comme donnée.

Le plafond est mesuré en tokens par projet, pas en tâches ou en temps. Le registre
compte les entrées et sorties réelles Qwen/Claude et le cache Claude ; les estimations
Qwen et la démonstration n'accordent aucun crédit. Une réservation conservative
précède l'appel Claude, avec blocage en cas de consommation inconnue ou supérieure
à la réserve. Le registre persiste après redémarrage et révision du design.
Voir la [politique de tokens](claude-policy.md) pour la formule et les limites des
estimations du fournisseur.

Les gabarits [Qwen studio](../examples/studio-qwen.json) et
[Claude optionnel](../examples/studio-claude.json) sont à adapter. Une clé Claude
ne doit pas être injectée dans le gabarit Qwen. L'utilisateur renseigne ses clés
dans l'environnement du Pod ; aucun secret n'est requis dans le dépôt ou le chat.

Ajouter un fournisseur n'améliore pas les poids de Qwen : mémoire structurée,
meilleurs prompts, évaluation et éventuel entraînement sont des mécanismes distincts.
Ces fichiers ne provisionnent aucun GPU et ne lancent aucune inférence par défaut.
