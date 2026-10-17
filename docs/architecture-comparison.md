# A6 — Ce que pcb_dev reprend d'atopile et de tscircuit

Étude ciblée du code et des documents officiels, consultés le **17 octobre 2026**.
Elle guide l'architecture de pcb_dev ; ce n'est ni un benchmark comparatif ni une
qualification physique des cartes produites par ces outils. Aucun code tiers
n'a été exécuté ou intégré pour cette étude.

Références examinées : atopile `619eda7f777558a3e500dbad9cc2941712881495` et
`tscircuit/core` `c8a0f74e841c12015b2bfd5196de605bf00dcfbd`. Les liens de code
ci-dessous sont épinglés pour distinguer cette observation de changements futurs.

## Intention et contraintes : atopile

L'[exemple officiel de diviseur](https://github.com/atopile/atopile/blob/619eda7f777558a3e500dbad9cc2941712881495/examples/equations/equations.ato)
sépare interfaces, composants, variables avec unités, connexions et relations.
Le besoin exprimé par le module supérieur contraint le diviseur ; plusieurs
relations équivalentes explicitent les inconnues. Le [sous-système de paramètres](https://github.com/atopile/atopile/blob/619eda7f777558a3e500dbad9cc2941712881495/src/faebryk/core/solver/README.md)
travaille avec domaines, ensembles, corrélations et expressions, et distingue ces
paramètres d'arguments de construction déterminés immédiatement. Cette différence
est structurante : le solveur peut réduire un domaine avant de sélectionner une pièce.

**Conséquence pour pcb_dev.** Une équation Python sûre évaluée dans un ordre de
dépendances n'est pas un solveur de contraintes dimensionnelles. Notre moteur
calcule des valeurs déclarées ; il ne faut pas présenter un champ calculé comme
un choix de composant qualifié ni convertir silencieusement cette valeur en
paramètre physique. Le prochain solveur devra conserver unités, intervalles,
conditions d'utilisation et provenance avant de propager un changement vers un
rating, une valeur réelle ou une règle de cuivre.

L'[exemple de réutilisation de layout](https://github.com/atopile/atopile/blob/619eda7f777558a3e500dbad9cc2941712881495/examples/layout_reuse/layout_reuse.ato)
compose plusieurs sous-modules et décrit une récupération du placement/routage
préparé dans KiCad. Il distingue donc réutilisation électrique et transfert de
layout. **Choix pour pcb_dev :** ne pas cocher B13/G16 avec une simple duplication
JSON. Un layout réutilisable devra porter son ancrage, ses transformations,
ses nets, son cuivre et les contrôles requis après instanciation.

## Représentation intermédiaire et phases : tscircuit

Le [cœur du circuit](https://github.com/tscircuit/core/blob/c8a0f74e841c12015b2bfd5196de605bf00dcfbd/lib/IsolatedCircuit.ts)
expose un Circuit JSON issu de la base et attend explicitement les effets
asynchrones pour terminer le rendu. Un premier rendu ou un état visuel disponible
ne signifie donc pas nécessairement que toutes les phases sont stabilisées.
Le [type de composant de base](https://github.com/tscircuit/core/blob/c8a0f74e841c12015b2bfd5196de605bf00dcfbd/lib/components/base-components/PrimitiveComponent/PrimitiveComponent.ts)
constitue un point commun aux composants et à leur représentation.

**Conséquence pour pcb_dev.** Conserver un modèle canonique partagé entre studio,
vérifications et générateurs. Les tâches longues doivent exposer un état distinct
de leur résultat final. Le studio applique déjà cette séparation avec une action
active, des snapshots et des preuves liées au design ; la génération de schémas
hiérarchiques devra conserver le même contrat pour toutes ses feuilles.

Le [test de règles de fabrication](https://github.com/tscircuit/core/blob/c8a0f74e841c12015b2bfd5196de605bf00dcfbd/tests/components/normal-components/board-manufacture-drc-property.test.tsx)
vérifie que plusieurs règles de carte sont propagées dans les données de routage
et les largeurs des pistes, puis produit un snapshot. C'est une preuve ciblée de
propagation logicielle. Elle ne démontre pas, à elle seule, l'exhaustivité d'un
ERC/DRC ou la qualification d'un fabricant. La [CLI officielle](https://docs.tscircuit.com/category/command-line-interface)
présente notamment build, check, simulation, export et snapshots.

La formule de la todo list selon laquelle tscircuit n'aurait pas d'ERC/DRC complet
ne doit pas devenir une affirmation générale actuelle de notre README. Les sources
consultées montrent des mécanismes de règles et de tests ; cette étude n'en mesure
pas la couverture électrique complète. Pour pcb_dev, les oracles restent les
contrôles explicitement exécutés, leurs rapports et leurs limites.

## Décisions pour le projet

| Sujet | Décision pcb_dev | Critère de validation |
| --- | --- | --- |
| Entrée utilisateur | Un prompt et un brief ; JSON comme représentation interne | Brief approuvé, exigences distinctes des hypothèses |
| Réutilisation électrique | Paquets data-only épinglés, références/nets déterministes | Reconstruction hors ligne et parité vérifiée |
| Réutilisation physique | Chantier distinct des paquets JSON | Géométrie/cuivre transférés et contrôles relancés |
| Contraintes | Évaluation sûre actuelle ; solveur dimensionnel à développer séparément | Unités/intervalles et dépendances explicites, aucun résultat inventé |
| Compilation | Une représentation canonique, plusieurs sorties et phases | Chaque sortie liée au hash du modèle et aux entrées exactes |
| Hiérarchie | Conserver les identités des composants à travers les feuilles | Export natif, chemins d'instances et ensemble des fichiers vérifiés |
| Règles fabricant | Propagation jusqu'au cuivre à vérifier explicitement | Comparer les règles déclarées, les pistes réelles et le DRC natif |
| IA | Qwen pilote des outils bornés ; aucune exécution de code généré arbitraire | Modifications transactionnelles et mesures sur des cas reproductibles |

Ces décisions sont des conclusions d'architecture pour pcb_dev, pas des propriétés
attribuées aux projets étudiés. Elles ne changent pas notre porte de fabrication :
un aperçu, un snapshot de test ou une déclaration du modèle ne vaut pas carte validée.
