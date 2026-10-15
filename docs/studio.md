# Studio : démarrer un PCB depuis une idée

Le studio fournit une conversation, un brief, un plan, des contrôles et des fichiers
de brouillon dans un projet persistant. Qwen est le manager. Claude est un avis
ponctuel demandé par l'humain, soumis au plafond de tokens du projet.

## Essayer l'interface sans modèle

Après installation (`python -m pip install -e .`) :

```sh
pcb-dev studio --project build/studio-demo --demo
```

Ouvrir <http://127.0.0.1:8788>. Saisir une idée puis examiner et valider le brief.
La démonstration utilise un scénario explicite et génère une carte générique vide.
Elle illustre le parcours et produit de vrais fichiers ; elle ne conçoit pas le
circuit demandé et ne crédite aucun token Qwen. Aucun modèle n'est connecté.

## Brancher le Qwen déjà servi sur le Pod

Le projet ne charge pas les poids et ne démarre pas de GPU. Un serveur compatible
Chat Completions avec appels d'outils doit déjà fonctionner. Utiliser l'identifiant
exact servi par ce serveur, indépendamment du nom du dossier de poids.

Si le studio et l'inférence tournent sur le même Pod :

```sh
pcb-dev studio --project /workspace/pcb-projects/ma-carte \
  --model IDENTIFIANT_EXACT_SERVI \
  --endpoint http://127.0.0.1:8000/v1
```

Pour les profils détaillés, adapter [studio-qwen.json](../examples/studio-qwen.json),
puis lancer :

```sh
pcb-dev model-check --config /workspace/config/qwen.json
pcb-dev studio --project /workspace/pcb-projects/ma-carte \
  --model-config /workspace/config/qwen.json
```

`model-check` déclenche un véritable appel de protocole, donc consomme de
l'inférence. Il vérifie la forme des appels d'outils, pas la compétence en
électronique. Le démarrage du studio seul n'effectue aucune inférence.
Le profil doit annoncer les capacités, le contexte et les limites réellement
supportés ; les valeurs de l'exemple ne sont pas un benchmark Qwen.

Si le studio accède au modèle depuis une autre machine, employer un endpoint
HTTPS authentifié et activer `offline=false`, `allow_network=true` dans le profil
et `--allow-network` dans la commande. Voir [Runpod et Claude](runpod-claude.md).

## Utiliser la conversation

1. Décrire la fonction, l'alimentation, les dimensions et les interfaces connues.
2. Répondre aux questions ; examiner séparément exigences, hypothèses et suggestions.
3. Valider le brief affiché. Cette action approuve son hash exact. Le modèle ne peut
   pas valider lui-même le besoin ; un changement du brief exige une nouvelle validation.
4. Laisser Qwen proposer son plan et employer les outils disponibles. Les erreurs
   d'outils lui sont renvoyées pour correction, dans un nombre d'étapes borné.
5. Examiner les contrôles et télécharger les fichiers. Un contrôle de métadonnées
   réussi ne vaut ni ERC/DRC natif ni autorisation de fabrication.

Le manager dispose de la création du modèle, de son inspection, des modifications
transactionnelles, du preflight, du build, des contrôles KiCad et de la simulation
explicite de passifs. Les chemins, exécutables et bindings sont choisis par l'hôte.
Ajouter `--bindings chemin/bindings.json` pour une bibliothèque épinglée compatible
avec le design ; aucun binding n'est inventé et aucun remplacement générique
n'est effectué si un binding réel est incomplet.

KiCad 9/10 et Ngspice sont optionnels et doivent être installés sur l'hôte pour
ces contrôles. Un moteur absent est signalé comme preuve manquante. La génération
ne route pas le cuivre ; les DRC non routées restent bloquantes.

## Claude : demande humaine et 10 % des tokens

Adapter [studio-claude.json](../examples/studio-claude.json) avec un modèle API
effectivement disponible. Fournir `ANTHROPIC_API_KEY` dans l'environnement du
processus, puis ajouter `--claude-config chemin/claude.json --allow-network`.
La clé ne doit figurer ni dans le JSON, ni dans le prompt, ni dans Git.

Le bouton d'avis Claude déclenche une action humaine distincte. Qwen ne possède
aucun outil permettant d'appeler Claude ; une panne Qwen ne déclenche aucune
bascule. L'avis est conservé et présenté à Qwen comme donnée consultative.

Le compteur compte les tokens d'entrée et de sortie rapportés par les API, ainsi
que les tokens de cache Claude. La règle est `Claude / (Qwen + Claude) ≤ 10 %`,
soit au plus `floor(Qwen / 9)` tokens Claude. Les tokens Qwen non mesurés ne créent
aucun crédit et le mode démo n'en crée jamais. Une réserve prudente est exigée
avant l'appel ; au début du projet, Claude peut donc être indisponible.

L'estimation d'entrée d'Anthropic n'est pas une mesure exacte. Le client ajoute
une marge et réserve la sortie maximale ; une réponse dépassant la réserve ou une
consommation inconnue conserve la réserve, met Claude en quarantaine et bloque les
appels suivants, même après redémarrage. Les compteurs API réels et les réserves
sont affichés séparément : une réserve n'est pas présentée comme une facture. Les
tokens d'entrée Claude comprennent entrée non cachée, lecture de cache et création
de cache ; le détail de création à cinq minutes/une heure n'est pas additionné une
seconde fois. Ce garde-fou local n'est pas une
garantie de facturation distante. Voir la [politique complète](claude-policy.md).

## Persistance et reprise

Relancer la même commande avec le même `--project` reprend le projet. La conversation,
le brief, le plan, le modèle, le manifeste et l'audit sont conservés. Le registre de
tokens est séparé des révisions : un redémarrage ou un retour sur un design ne
recrée pas de crédit. Un appel interrompu ne rejoue aucun outil automatiquement.

Le projet est verrouillé pendant les écritures ; une deuxième action est refusée
pendant le travail du modèle. Les compteurs et l'historique sont locaux et ne
constituent pas un système de comptabilité résistant à un administrateur qui
modifie ou remplace les fichiers. Sauvegarder le dossier complet sur le volume
persistant du Pod. Un dossier de démonstration ne peut pas être repris en mode réel.

## Accès depuis le navigateur hors du Pod

Le défaut est loopback. Pour un proxy HTTPS Runpod, définir un secret d'accès au
studio dans `PCB_STUDIO_TOKEN` (au moins 24 caractères ASCII imprimables), puis :

```sh
pcb-dev studio --project /workspace/pcb-projects/ma-carte \
  --model-config /workspace/config/qwen.json \
  --host 0.0.0.0 --port 8788 --auth-env PCB_STUDIO_TOKEN \
  --public-origin https://VOTRE-POD-8788.proxy.runpod.net
```

L'origine doit correspondre exactement à l'URL HTTPS du proxy. Saisir le secret
d'accès dans l'écran de connexion ; il reste en mémoire du navigateur. Il est
distinct des clés Qwen/Claude et ne doit pas être mis dans l'URL. Le serveur vérifie
Host, Origin et l'authentification ; il ne fournit pas lui-même le certificat TLS.
Le serveur est prévu pour un seul projet et un utilisateur de confiance, sans
gestion de comptes, service de supervision ni isolation système multiutilisateur.

## Limites de cette alpha

Le parcours logiciel est testable avec un fournisseur scénarisé et des moteurs
natifs. Aucun Qwen réel n'a encore été qualifié dans ce dépôt, et aucun appel
Claude réel n'a été effectué pour développer cette version. La sélection sourcée
de composants, l'extraction de datasheets, le routage, l'entraînement du modèle et
la carte USB-C/MCU/LED fabricable restent des chantiers distincts.
