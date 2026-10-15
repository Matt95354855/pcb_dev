# Claude : demande humaine et budget de tokens par projet

Qwen pilote le projet sur son endpoint configuré. Claude fournit un avis à la
demande de l'humain ; une erreur Qwen ne déclenche jamais de bascule et aucun
outil accessible à Qwen ne permet d'appeler Claude. Le manager demande une réponse
texte, sans outils ni pouvoir d'approuver une fabrication.

Le constructeur `ClaudeProvider(config, ledger=ledger)` n'effectue aucune requête.
Chaque appel à `review(messages, max_tokens=..., human_authorized=True)` doit
provenir de l'action humaine dédiée. L'autorisation n'est conservée ni dans le
constructeur ni pour l'appel suivant. La méthode générique `complete` refuse elle
aussi toute demande qui ne porte pas cette autorisation ponctuelle.

## Configuration et transport

Adapter [studio-claude.json](../examples/studio-claude.json) avec l'identifiant
exact du modèle réellement accessible. `ClaudeConfig` exige un modèle explicite
et fournit `from_dict`/`to_dict`. La clé est lue dans `ANTHROPIC_API_KEY`, ou dans
l'entrée d'environnement nommée par `api_key_env`, au moment d'une requête. Le JSON
contient le nom de cette entrée, jamais la clé.

Le transport utilise directement [l'API Messages officielle](https://platform.claude.com/docs/en/api/messages/create),
avec `x-api-key` et `anthropic-version: 2023-06-01`. L'accès réseau doit être
explicitement activé. L'endpoint exige HTTPS, le port standard, un hostname exact
de `allowed_hosts` et un chemin Messages connu ; identifiants dans l'URL, query
strings, fragments et redirections sont refusés. Les proxies d'environnement
sont désactivés et les certificats TLS restent vérifiés par la bibliothèque
standard Python.

Les requêtes et réponses sont bornées. Les JSON à clés dupliquées ou valeurs non
finies sont refusés. Les rôles, blocs de contenu, raisons d'arrêt et éventuels
appels d'outils sont validés ; un contenu tronqué ou un outil non déclaré n'est
pas exécuté. Le manager utilise exclusivement `review`, qui n'expose aucun outil.
Il n'y a ni retry automatique ni appel de démarrage ou de qualification en ligne.

## Calcul de l'enveloppe

Soit `Q` le nombre de tokens Qwen d'entrée et de sortie réellement rapportés et
validés par l'API, et `C` le nombre de tokens Claude réellement rapportés :

```text
C / (Q + C) <= 0,10
C <= floor(Q / 9)
disponible = floor(Q / 9) - C - réservations non résolues
```

Une revue est refusée avant l'inférence si sa réservation ne tient pas dans
l'enveloppe. Un projet neuf commence sans crédit Claude. Le nombre d'appels et
le coût monétaire ne remplacent pas ce calcul en volume de tokens.

Seuls les `ModelReply` Qwen portant `usage_reported=True` et des compteurs entiers
valides créditent `Q`. Les estimations en octets du client, le mode démo et les
usages absents ne créent aucun crédit. Un usage Qwen absent incrémente
`unknown_qwen_calls` ; Qwen peut continuer, sous ses propres budgets, mais cette
activité ne rend pas Claude disponible.

Pour Claude, `C` additionne `input_tokens`, `output_tokens`,
`cache_read_input_tokens` et `cache_creation_input_tokens`. L'[API de cache
officielle](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)
distingue les trois catégories d'entrée. `cache_creation` détaille l'agrégat de
création par durée : ce détail est vérifié et n'est jamais additionné une seconde
fois. Des usages contradictoires ou manquants ne deviennent pas des zéros connus.

## Estimation, réservation et incertitude

Après autorisation et vérification du crédit disponible, le client interroge
`/v1/messages/count_tokens` avec les entrées structurées du modèle choisi. Cet
appel ne crée aucun crédit de tokens. Anthropic indique que [le comptage est une
estimation et peut différer de l'usage réel](https://platform.claude.com/docs/en/build-with-claude/token-counting).
Cet endpoint est gratuit selon cette documentation ; il transmet tout de même
le contenu à Anthropic et exige donc la même autorisation humaine.

La réservation prend le plus grand du comptage majoré par `count_margin_ratio`
et de la taille UTF-8 sérialisée des entrées, ajoute `count_margin_tokens`, puis
la sortie maximale demandée. Les marges doivent rester positives. Cette politique
est prudente ; elle ne constitue pas une borne mathématique sur les compteurs du
serveur ni une garantie de facture distante.

La réservation est persistée avant l'appel Messages. Une seule revue Claude peut
être active par projet. À la réception d'un usage valide contenu dans la réserve,
le registre comptabilise les tokens API et libère seulement la différence connue.
Un résultat rejeté pour contenu ou outil invalide conserve sa consommation API
déjà connue, même si aucun avis n'est présenté.

Un timeout, un résultat non interprétable ou un usage final absent conserve la
réserve et met Claude en quarantaine. Si l'usage réel dépasse la réserve, les
compteurs réels sont enregistrés, la réserve est conservée comme garde-fou
supplémentaire et Claude est également mis en quarantaine. Le rapport indique
`quota_verified: false` ; il ne prétend pas que les 10 % ont été respectés. Aucune
autre revue n'est admise automatiquement, même si Qwen apporte ensuite des tokens.

Une réservation restée en attente après un arrêt brutal demeure bloquante au
redémarrage. Il n'existe pas de bouton ou d'outil modèle permettant de l'effacer,
de rembourser un timeout ou de réinitialiser les tokens consommés. La résolution
comptable d'un appel incertain nécessite une future procédure humaine sourcée,
et ne doit pas être simulée en supprimant le registre.

## Persistance et preuves

`TokenLedger(project_dir)` utilise `token-ledger.json` et un verrou OS dédié,
indépendamment des révisions de design. Chaque lecture et modification relit
l'état sous verrou : `flock` sur Linux/macOS, `msvcrt.locking` sur Windows.
Les écritures passent par un fichier temporaire synchronisé puis un remplacement
atomique. Les instances du manager et de Claude peuvent partager le même dossier
sans écraser leurs compteurs. Rollback et reprise de design ne recréent pas de crédit.

`snapshot()` sépare compteurs API, réserves, part mesurée de Claude, usages Qwen
inconnus, disponibilité et statut de quarantaine. Une part mesurée faible ne
prouve pas le respect du plafond si une consommation Claude reste inconnue.
Les erreurs transport et le registre ne sérialisent ni clés, ni en-têtes, ni
prompts, ni corps de réponse d'erreur.

Ce registre local ne résiste pas à un administrateur qui remplace les fichiers.
Il faut sauvegarder le dossier complet sur le stockage persistant du Pod et
configurer les contrôles de consommation adaptés chez le fournisseur. Les tests
du dépôt emploient des réponses synthétiques, y compris pour les erreurs réseau ;
ils ne constituent aucune qualification d'un modèle réel ou de sa facturation.
