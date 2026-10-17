# Mesurer le parcours Qwen avant de lui confier un projet

Le banc N6 teste le manager sur des projets isolés, avec des consignes et des
critères automatiques versionnés. Il prépare la qualification du Qwen réellement
servi sur le Pod ; il ne remplace pas une validation physique de carte.

Après avoir adapté le profil au serveur et au modèle effectivement servis :

```sh
pcb-dev model-check --config /workspace/config/qwen.json
pcb-dev model-eval --config /workspace/config/qwen.json \
  --output /workspace/evaluations/qwen-essai-001 --approve-test-briefs
```

Ces deux commandes déclenchent de vrais appels au modèle. Pour un endpoint HTTPS
distant, ajouter `--allow-network` et activer la permission dans le profil.
Aucun appel Claude n'est configuré dans ce banc. Le constructeur et la lecture du
profil seuls ne lancent aucune inférence. Ne pas utiliser les gabarits sans
remplacer leur identifiant de modèle et leur endpoint.

`--approve-test-briefs` autorise le banc à approuver le hash d'un brief sans question
uniquement dans les dossiers de test nouvellement créés. Il n'approuve aucun projet
utilisateur existant. Une clarification demandée arrête le cas, sans réponse
inventée. Le répertoire de sortie doit être neuf.

## Scénarios et preuves

- `diagnostic-draft` : produire un brouillon générique de diagnostic depuis une idée.
- `ideal-divider` : réaliser le modèle d'un diviseur résistif idéal selon les
  exigences du scénario et produire de vrais fichiers de prévisualisation.

Employer `--case ideal-divider` pour sélectionner un scénario ; l'option est
répétable. Le rapport sépare les critères observés dans le modèle, le preflight,
les artefacts, les questions et les erreurs. Il conserve les tokens rapportés,
les consommations inconnues et le temps écoulé. `evaluation.json` est exploitable
automatiquement ; `evaluation.md` sert à la revue. Les projets et leurs fichiers
permettent d'inspecter le résultat au-delà du seul score.

Les limites `--max-steps` et `--max-tokens` s'appliquent à chaque tour du manager.
Le profil du fournisseur porte également ses propres limites d'appels et de tokens.
Les données et les prompts conservés restent sur le stockage choisi ; un endpoint
modèle distant reçoit les requêtes explicitement autorisées.

## Comparer et interpréter

Conserver l'identifiant exact du modèle, sa quantification, le serveur, son chat
template/parser d'outils et le profil utilisés avec chaque mesure. Exécuter les
mêmes scénarios dans de nouveaux dossiers après un changement de modèle ou de
prompt. Les métriques réelles ne sont disponibles qu'après ces exécutions ; aucun
résultat Qwen n'est prérempli dans le dépôt.

Une réussite mesure uniquement le contrat logiciel du scénario. Elle ne prouve
ni la compétence générale du modèle, ni une vraie référence commerciale, ni ERC/DRC
à zéro, ni routage, ni fabricabilité. Les tests automatisés du banc utilisent des
fournisseurs scénarisés identifiés ; leurs résultats ne sont pas des scores Qwen.
Le banc complet sur des cartes physiques de référence reste à développer.
