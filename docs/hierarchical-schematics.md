# Schémas KiCad hiérarchiques

Le mode hiérarchique produit une feuille par bloc du modèle, y compris les blocs
intermédiaires sans composant. Chaque composant apparaît dans la feuille de son
bloc ; les sous-blocs sont représentés par des rectangles de feuilles.

```sh
pcb-dev build examples/library_divider/port_design.json \
  --bindings examples/library_divider/port_bindings.json \
  --hierarchical --require-physical-ports --output build/divider-hierarchy
pcb-dev verify build/divider-hierarchy
```

Ouvrir le projet KiCad sous `build/divider-hierarchy/kicad/`. Le manifeste et
`build-lock.json` recensent la racine et les fichiers de feuilles. Copier tout le
répertoire, bibliothèques comprises, pour déplacer le projet. Le mode s'applique
également aux brouillons génériques ; il ne qualifie pas leurs symboles ou pads.
Dans le studio, ajouter `--hierarchical` à la commande de lancement.

## Identités et connectivité

Les noms de fichiers et UUID sont déterministes et distinguent les chemins de
blocs complets. Les instances de symboles, les chemins des footprints et les
chemins de feuilles de la netlist suivent cette hiérarchie. Les références,
valeurs, positions PCB et nets du modèle sont conservés.

Les connexions utilisent les noms globaux existants. Cette première version
organise réellement les fichiers et instances ; elle n'ajoute pas encore des
interfaces électriques par pins de feuilles, des bus ou une réutilisation d'un
même fichier enfant sous plusieurs instances. Le mode plat reste disponible et
constitue le défaut pour les commandes existantes. Les grandes arborescences doivent
être groupées : cet aperçu est borné à 256 feuilles et 21 sous-blocs directs par
page, avec un refus explicite si la disposition dépasse ces limites.

## Preuves couvrant toutes les feuilles

Une empreinte du seul fichier racine ne suffit pas à vérifier un schéma
hiérarchique. L'adaptateur natif construit donc un ensemble borné des fichiers
référencés et vérifie leur contenu avant et après ERC et export de netlist.

Pour une feuille unique, `input_sha256` conserve le hash historique du fichier.
Pour une hiérarchie, il représente l'ensemble racine + enfants. Les métadonnées
conservent aussi le hash brut de la racine et la liste des fichiers avec leurs
empreintes. La porte native compare les entrées ERC/export/parité et revérifie
l'ensemble courant après l'export. Enfant absent ou modifié, cycle, chemin externe
ou symlink invalident la preuve ; aucun enfant n'est silencieusement ignoré.

Les commandes `native-check` et l'outil correspondant du manager partagent la même
porte de validation. Les rapports ERC/DRC restent distincts de la parité et de la
qualification physique. Une DRC non routée demeure bloquante ; créer des feuilles
ne route aucune piste.

Le smoke natif hiérarchique est défini dans la matrice KiCad 9/10, avec export XML,
parité des vrais composants, contrôle des chemins et mutations d'enfants. Les
unit tests de génération ne constituent pas une exécution native.
