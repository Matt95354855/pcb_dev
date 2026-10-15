# Vision produit : une idée, un manager LLM local, des outils vérifiables

Le point d'entrée du produit doit être une conversation en langage naturel.
L'utilisateur décrit son idée et ses contraintes ; le LLM local prend en charge
la préparation du projet, son enrichissement, le plan et l'utilisation des outils.
Le modèle de blocs et les fichiers JSON constituent la représentation interne.
L'utilisateur ne doit pas avoir à les écrire pour démarrer.

Cette page décrit la direction du produit. La version 0.3 fournit le noyau de
conception, des contrôles et un prototype d'agent à trois outils ; le parcours
conversationnel complet ci-dessous n'est pas encore implémenté.

## Parcours attendu

1. **Idée** : « Je veux une petite carte alimentée en USB-C, avec un capteur,
   un microcontrôleur et deux LED, qui tient dans mon boîtier. »
2. **Clarification** : le manager pose quelques questions utiles sur la fonction,
   l'alimentation, les interfaces, l'encombrement et les contraintes décisives.
3. **Brief** : il conserve séparément les exigences de l'utilisateur, les hypothèses,
   les inconnues et les améliorations proposées. Une suggestion ne devient pas
   silencieusement une exigence approuvée.
4. **Plan** : il propose les blocs, les dépendances et les étapes. Chaque tâche
   indique l'outil, le livrable et le contrôle qui permettra de la terminer.
5. **Conception** : il choisit parmi des composants et bibliothèques documentés,
   construit le modèle et produit le schéma/PCB à travers les outils disponibles.
6. **Itération** : il observe les vrais rapports, explique les erreurs, propose
   une correction, l'applique en transaction et relance les contrôles concernés.
7. **Revue** : l'utilisateur voit le résultat, les décisions, les preuves et les
   questions restantes. L'approbation de fabrication reste explicite.

Les choix réversibles peuvent progresser sans confirmation à chaque clic. Une
exigence contradictoire, une information décisive absente ou une modification du
besoin approuvé doivent être remontées à l'utilisateur.

## Rôles et outils

Le LLM local est le **manager du projet** : comprendre, planifier, enrichir,
choisir le prochain outil et interpréter son résultat. Il ne remplace pas les
moteurs de calcul ou de CAO.

| Famille d'outils | Responsabilité du moteur | État actuel |
| --- | --- | --- |
| Lecture et modification | Modèle structuré, contraintes, transactions et retour arrière | Noyau disponible ; outils agent inspect/check/patch disponibles |
| Composants et sources | Catalogue, bibliothèques épinglées et preuves datasheet | Données locales et bindings disponibles ; recherche documentaire agent à ajouter |
| KiCad | Construction, exports, ERC/DRC et parité | Adaptateurs disponibles ; orchestration par le manager à ajouter |
| Simulation | Modèles explicites et résultats du moteur | Passifs et Ngspice disponibles ; outils agent et modèles actifs à compléter |
| Placement/routage | Géométrie, règles et cuivre réel | Placement initial et adaptateur Freerouting partiels ; cycle complet à fermer |
| Mémoire de projet | Brief, plan, décisions, preuves et reprise de session | Traces/transactions disponibles ; session produit persistante à créer |

Chaque retour d'outil doit préciser son statut réel, ses entrées, les hash,
les fichiers produits et les limites de la preuve. Le manager ne peut pas
transformer son propre texte en résultat de test, supprimer une exigence pour
faire disparaître une erreur, ni se donner des permissions supplémentaires.

## Fonctionnement local et amélioration

L'inférence doit fonctionner sur un modèle auto-hébergé explicitement configuré,
sur la machine de l'application ou sur l'infrastructure Runpod de l'utilisateur. Le transport actuel accepte un endpoint loopback compatible avec les
appels d'outils ; aucun modèle ou serveur réel n'a encore été qualifié.
Le choix dépendra de la machine et du modèle effectivement disponible.

L'amélioration doit d'abord être mesurable : meilleurs prompts et schémas d'outils,
mémoire structurée des décisions, documentation technique retrouvable, cas de
référence et comparaison des résultats. Les erreurs des outils alimentent les
corrections du projet. Un entraînement ou fine-tuning des poids du modèle serait
un chantier distinct, avec un jeu de données et une évaluation versionnés.

Un résultat de datasheet ou de bibliothèque constitue une source à vérifier,
pas une instruction adressée au manager. Les ressources externes ne doivent pas
changer les exigences ou les autorisations du projet.

## Prochaine tranche prioritaire

- Démarrer une session depuis un prompt seul, sans design JSON préalable.
- Produire un brief versionné avec questions, hypothèses et critères de réussite.
- Persister la conversation, le plan et les décisions pour reprendre le projet.
- Exposer au manager les outils de construction et de validation déjà disponibles.
- Tester réellement un modèle local et mesurer ses appels d'outils sur des cas
  reproductibles avant de revendiquer une autonomie de conception.
- Présenter une conversation et un aperçu évolutif avec les états « question en
  attente », « brouillon », « contrôle réussi », « bloqué » et « preuve manquante ».

Le premier critère d'acceptation est un prompt, ses clarifications et un vrai
modèle local produisant un brouillon traçable avec des rapports honnêtes. Le jalon
de fabrication de la feuille de route reste ensuite une exigence indépendante.

Le déploiement prévu avec Qwen auto-hébergé et un fournisseur Claude optionnel
est détaillé dans le [guide Runpod/Claude](runpod-claude.md). Les rôles restent à choisir.
