# État de la todo list PCB

La priorité produit est désormais le parcours **prompt → brief → manager LLM local
→ outils → preuves → itérations**. Le JSON est un format interne. Le noyau ci-dessous
sert cette expérience ; le studio orchestre désormais la première boucle complète,
avec validation humaine du brief et reprise persistante.
Voir la [vision détaillée](prompt-first.md).

Référence : « Outil de création de PCB — Todolist complète », fournie au démarrage.
État de la version **0.4.0 alpha**, au 15 octobre 2026. Les numéros ci-dessous
reprennent cette liste pour permettre une reprise précise.

**Livré** décrit le comportement logiciel disponible et testé. **Partiel**
signifie qu'une base fonctionne mais que la tâche d'origine comporte des
exigences supplémentaires. **À faire** ne doit pas être interprété comme
couvert par un autre module. Les tests unitaires ne valident pas une carte physique.

## A — Fondations

| IDs | État | Preuve / suite |
| --- | --- | --- |
| A1–A5 | Livré pour l'alpha | Petites équipes, basse tension 2–4 couches, KiCad, Python, JSON/CLI et revue locale ; ADR dans architecture.md |
| A6 | À faire | Analyse comparative détaillée du code atopile/tscircuit |
| A7 | Livré | Packaging, licence MIT, tests, conventions de contribution et workflows CI |
| A8 | Livré pour versions testées | Diagnostic et conteneurs épinglés ; smoke natif 9.0.9/10.0.2 passé sur la base 0.2.0, incréments testés séparément |
| A9 | Livré comme critères | Reproductibilité du modèle ; jalon physique ERC/DRC zéro encore ouvert |

## B — Modèle de blocs

| IDs | État | Preuve / suite |
| --- | --- | --- |
| B1–B6 | Livré pour données déclarées | Modèle strict, ports, arbre, connexions et graphe avec cycles/ordre/impact |
| B7–B8 | Partiel | Héritage, équations sûres, contraintes et tolérances ; pas de solveur dimensionnel ni propagation implicite vers les champs physiques |
| B9 | Partiel | Sélection locale par paramètres, statut et stock déclaré ; pas de fournisseurs temps réel |
| B10–B11 | Livré pour paquets locaux | Instanciation avec préfixes nets/références et variantes fit/DNP |
| B12–B13 | À faire | Schéma réellement hiérarchique et réutilisation de placement/routage qualifié |
| B14 | Livré | Gel contextuel détectant changements hérités, amont et connexions |
| B15 | Partiel | Exemples alimentation/MCU/LED et circuits passifs ; bibliothèque de blocs qualifiés à construire |
| B16 | Partiel | Paquets JSON locaux, versions exactes, SHA-256 et lockfile ; registre distant/publication à faire |
| B17 | Livré | Sérialisation JSON déterministe et diffable |
| B18 | Partiel | Tests logiciels et simulation explicite de passifs ; pas de qualification de chaque bloc physique |

## C — Vérifications en amont

| IDs | État | Preuve / suite |
| --- | --- | --- |
| C1 | Livré pour métadonnées | Tension au pire cas, courant, type, direction, logique, protocole, polarité, impédance |
| C2–C3 | Partiel | Budgets de courant, calculs déclarés, ordre/délais de séquencement ; comportement réel/échauffement à simuler |
| C4 | Partiel | Ports requis, réalisation explicite par pins locales, absence de source et conflits ; pull-ups/pinmux/datasheets à compléter |
| C5 | Partiel | Adresses I²C ; pinmux MCU/DMA/timers à implémenter |
| C6 | À faire | Règles de découplage par vraie broche et distance de placement |
| C7 | Livré sur limites fournies | Ratings tension/courant/puissance avec déclassement déclaré |
| C8–C9 | À faire | Ancrage datasheet et revue IA spécialisée de conception |
| C10 | Partiel | Stock local explicite/absent, jamais inventé ; connecteurs fournisseurs à ajouter |
| C11 | Partiel | Inventaire de pins/pads et comparaison de déclarations ; qualification datasheet réelle à faire |
| C12–C13 | À faire | Isolation haute tension et estimation complète de coût/faisabilité |
| C14 | Partiel | Adaptateur ERC natif et smoke CI ; absence locale clairement signalée |
| C15 | Livré | Rapports stables JSON/Markdown et constats par chemin de bloc |
| C16 | Partiel | Gate générique exigeant preuves présentes/complètes ; build autorisé uniquement comme aperçu, pas comme layout qualifié |

## D — Composants et bibliothèques

| IDs | État | Preuve / suite |
| --- | --- | --- |
| D1 | Partiel | Catalogue JSON local + inventaires de bibliothèques ; liens SPICE/datasheet/3D unifiés à compléter |
| D2 | À faire | API fournisseurs, cache distant et quotas |
| D3 | Partiel | Import et génération avec bibliothèques réelles épinglées, tables locales autonomes ; multiunités/double face/3D à compléter |
| D4–D5 | À faire | Extraction datasheet et générateur IPC-7351 |
| D6 | Partiel | Contrôles géométriques limités de pads/courtyard/silkscreen ; pas de conformité IPC revendiquée |
| D7 | À faire | Alignement et validation des vrais modèles STEP |
| D8 | Partiel | Alternatives locales et sélection par bornes ; substitution qualifiée à faire |
| D9 | Livré | Statuts approved/review/avoid/banned et sélection conservatrice |

## E–F — Schéma et simulation

| IDs | État | Preuve / suite |
| --- | --- | --- |
| E1–E2 | Partiel | Schéma plat autonome générique ou vrais symboles épinglés ; hiérarchie et mise en page avancée à faire |
| E3 | Livré | Noms explicites stables de nets globaux |
| E4 | Partiel | Références explicites stables ; annotation automatique à ajouter |
| E5 | À faire | Import d'un schéma en arbre de blocs |
| E6 | Partiel | Export SVG natif ; PDF et revue visuelle réelle à compléter |
| E7 | Partiel | Comparaison stricte modèle/netlist/PCB ; pas de resynchronisation bidirectionnelle |
| F1 | Livré pour R/C/L/V/I explicites | Génération SPICE sûre ; aucun modèle MCU ou semi-conducteur inventé |
| F2–F3 | Partiel | Ngspice DC/AC/transitoire et scope par bloc ; tests de circuits passifs, modèles réels restants |
| F4 | Partiel | Monte Carlo uniforme reproductible et coins d'intervalles sur équations ; pas de bornes certifiées |
| F5–F6 | Partiel | Estimations thermiques à un coefficient et chute de tension ; géométrie réelle à intégrer |
| F7–F8 | À faire | Intégrité du signal et critères de simulation reliés au gate de design |

## G–H — Layout et fabrication

| IDs | État | Preuve / suite |
| --- | --- | --- |
| G1–G5 | Partiel | PCB générique ou vraies empreintes épinglées, contour rectangulaire, 2/4 couches et centres déclarés ; vrai stackup/placement/routage à compléter |
| G6 | Partiel | Formule externe IPC-2221 documentée ; pas de solveur IPC-2152 ni validation du cuivre réel |
| G7 | À faire | Paires différentielles/impédance/longueurs |
| G8 | Partiel | DSN → Freerouting épinglé → SES ; export DSN depuis KiCad et import SES non automatisés |
| G9–G12 | À faire | Routage priorisé, plans de masse/vias, placement découplage et détails fabricant |
| G13 | Partiel | Textes d'aperçu stables ; règles de sérigraphie complètes à faire |
| G14–G16 | À faire | Resynchronisation manuelle, optimisation de routage et layouts réutilisables |
| H1–H2 | Partiel | Profils conservateurs JLCPCB/PCBWay/OSH Park ; aucune révision fournisseur certifiée |
| H3–H6 | À faire | Finitions/coûts, assemblage réel, corrections rotation JLCPCB et panneautisation |

## I — Sortie

| IDs | État | Preuve / suite |
| --- | --- | --- |
| I1 | Partiel | DRC JSON natif avec exclusions/contrôles ignorés bloquants ; carte réelle à valider |
| I2 | Partiel | Inspection d'empreintes limitée ; DFM sur cuivre/masque/perçages réels à faire |
| I3–I4 | Partiel | Références, valeurs, noms d'empreintes, types et pin nets modèle/netlist/PCB, NC explicites et liens ports ↔ pins locaux ; exposition hiérarchique et qualification datasheet à compléter |
| I5–I7 | À faire | Respect des contraintes sur le layout réel |
| I8 | Partiel | Exports natifs Gerber/Excellon/CPL/STEP ; pas de livraison finale autorisée |
| I9 | À faire | Réimport et comparaison géométrique des Gerber |
| I10–I11 | Partiel | BOM et CPL indicatifs ; stock, rotations et revue visuelle à qualifier |
| I12 | À faire | Validation IPC-D-356 |
| I13 | Partiel | Export STEP natif ; vrais modèles 3D et rendu final à qualifier |
| I14–I16 | Partiel | Rapports, gates fermés sur preuves absentes, hash et archives d'aperçus ; archive de fabrication complète encore impossible |

## J–L — Expérience, qualité et documentation

| IDs | État | Preuve / suite |
| --- | --- | --- |
| J1–J4 | Partiel | Studio conversationnel, brief/plan/contrôles, visionneuse hors ligne et aperçu de centres ; édition CAO interactive à faire |
| J5 | Partiel | Fichiers natifs exportables, aperçu de centres ; pas de viewer CAO intégré complet |
| J6–J8 | Partiel | Manager borné, brief approuvé par hash, corrections transactionnelles et audit ; diff visuel de modifications à faire |
| J9 | Livré pour le périmètre alpha | CLI checks/build/graph/inspect/archives/paquets/agents/adaptateurs |
| J10–J11 | À faire | Extension VS Code et collaboration |
| K1–K3 | Partiel | Tests logiciels, reproductibilité et matrice native 9/10 ; cartes réelles de référence à qualifier |
| K4 | Partiel | Entrées absurdes/cycles/extrêmes testés ; fuzzing continu à ajouter |
| K5 | Livré pour sessions locales | Journal chaîné des lots, motifs et undo ; auth multiutilisateur à faire |
| K6 | Partiel | Verrous par hash/version des paquets et bibliothèques ; chaîne physique complète à verrouiller |
| K7 | Partiel | Sous-processus sans shell, limites de temps/sorties ; isolation OS/réseau à exécuter en conteneur |
| K8 | Partiel | Paquets data-only et hash ; signatures et registre de revue à ajouter |
| K9 | Partiel | Git, snapshots transactionnels, archives ; procédures d'exploitation à compléter |
| L1 | Partiel | Documentation de hiérarchie/rails/constats générée ; bring-up/pinouts complets à faire |
| L2–L3 | À faire | Guide d'assemblage physique et fiche PDF signée |
| L4–L5 | Livré pour l'alpha | README FR/EN, tutoriel, modèle, blocs/paquets, adaptateurs et garde-fous |

## M–N — Avancé et IA

| IDs | État | Preuve / suite |
| --- | --- | --- |
| M1–M8 | À faire | RF, haute vitesse, puissance, HDI, firmware, EMC, normes et commande fabricant |
| N1.1–N1.4 | Partiel | Protocole fournisseur, chat/tool calling, endpoint local/distant explicite, profil et quotas ; streaming à ajouter |
| N1.5 | Partiel | Qwen manager et revue Claude sur demande humaine, plafond persistant de 10 % des tokens ; aucune bascule automatique |
| N1.6–N1.7 | Partiel | Réseau externe interdit par défaut, local loopback, self-test explicite ; isolation OS et benchmark réel à qualifier |
| N2 et annexes | À faire | Aucun GPU/cloud loué, aucun poids téléchargé, aucun modèle spécifique imposé |
| N3.1–N3.5 | Livré pour propositions de modèle | Budgets, schémas d'outils, arguments stricts, copies transactionnelles ; pas d'outil coûteux/irréversible exposé |
| N3.6–N3.7 | Partiel | Réparation de métadonnées, arrêt sans progrès et protection du gel ; réparations CAO natives autonomes à ajouter |
| N3.8–N3.11 | Partiel | Brief/plan/conversation persistants, revue Claude humaine séparée ; spécialistes et mémoire documentaire à compléter |
| N3.12 | Livré pour le studio | Reprise de conversation, brief, plan, preuves et registre de tokens ; appels interrompus non rejoués |
| N4.1–N4.2 | Partiel | Schémas stricts et catalogue disponible ; ancrage fournisseur de chaque référence encore requis |
| N4.3–N4.6 | À faire | Datasheets sourcées, citations de chaque décision, gabarits experts et banc few-shot physique |
| N5.1–N5.3 | Livré dans outils exposés | Pas de waiver/gate/export/shell pour le modèle ; données externes traitées comme données |
| N5.4 | Partiel | Clés par nom de variable d'environnement, plafonds et masquage ; coffre chiffré/rotation à intégrer |
| N5.5 | Partiel | Traces entrées/sorties/outils/décisions avec secrets masqués ; conservation centralisée à ajouter |
| N5.6 | Partiel | Outils de design sans fichier/réseau/shell ; conteneur système externe requis pour vraie sandbox |
| N5.7 | Livré pour métadonnées | Refus des régressions et des altérations de gel/exigences protégées |
| N6–N7 | Partiel | Studio et banc de scénarios logiciels versionnés disponibles ; mesures d'un vrai Qwen et benchmark physique encore à faire |

## Prochain incrément recommandé

1. Brancher le Qwen effectivement servi sur le Pod, qualifier ses appels d'outils et ses résultats sur des cas de référence.
2. Conserver la matrice native KiCad/Ngspice et les preuves d'orchestration du manager.
3. Sélectionner des références commerciales réelles et qualifier les bibliothèques déjà importables.
4. Lier chaque port de bloc aux pins physiques et générer un schéma hiérarchique.
5. Produire le premier layout routé, puis démontrer ERC/DRC zéro.
6. Vérifier DFM et relire les Gerber/BOM/CPL exportés avant d'ouvrir la livraison finale.

Le nombre de cases couvertes ne remplace pas ces preuves de bout en bout.
