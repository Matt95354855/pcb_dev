# Validation des livraisons

## Base 0.1.0 et correctif 0.1.1

Vérification locale du 15 octobre 2026 : Python 3.14 sur macOS.

| Contrôle | Résultat |
| --- | --- |
| `python -m unittest discover -s tests -q` | 250 tests réussis |
| `ruff check src tests` avec Ruff 0.14.0 | Aucune erreur |
| Exemple USB/MCU/LED | Préflight du modèle réussi ; règles fabricant non vérifiées signalées |
| Exemples surtension et surcharge | Bloqués comme attendu |
| Construction/manifestes/archives | Intégrité et reproductibilité testées |
| Agent scripté via CLI | Proposition sauvegardée ; original conservé ; fabrication non approuvée |
| Génération SPICE RC | Netlist explicite produite ; aucune simulation locale revendiquée |
| Visionneuse | Ouverture et navigation par bloc vérifiées dans un navigateur |

Les tests des adaptateurs natifs utilisent des doublures explicites. Aucun
KiCad, Ngspice ou Freerouting réel n'était installé localement. Les workflows
GitHub exécutent séparément KiCad 9/10 et Ngspice ; leurs résultats sont visibles
dans [GitHub Actions](https://github.com/Matt95354855/pcb_dev/actions).

La base publiée **0.1.1** a ensuite passé toute la matrice Python Linux/macOS/Windows
([exécution CI](https://github.com/Matt95354855/pcb_dev/actions/runs/38706705614)) et les
tests natifs KiCad 9.0.9, KiCad 10.0.2 et Ngspice
([exécution native](https://github.com/Matt95354855/pcb_dev/actions/runs/38706705683)).

Le smoke KiCad attend des violations de connexions non routées sur l'aperçu.
Son succès signifie que les fichiers se chargent, que les nets sont conservés
et que les constats natifs sont correctement retenus. Il ne signifie pas que
la carte passe DRC ni qu'elle est fabricable.

## Incrément 0.2.0 : bibliothèques réelles épinglées

Vérification locale du 15 octobre 2026 : **317 tests réussis**, Ruff 0.14.0 sans
erreur, sous Python 3.14 sur macOS. Les régressions couvrent notamment les liaisons
complètes, les types électriques, la géométrie des pads/perçages, les rotations,
les ancres du schéma, les octets sources/licences capturés et la reconstruction
hors ligne identique. Deux revues croisées du code et de la documentation ont été
effectuées en complément.

Le paquet Python a également été installé dans un environnement neuf pour
construire le diviseur résistif avec ses bibliothèques officielles. Son manifeste
vérifie 34 fichiers. L'ouverture de la visionneuse et le statut `LIBRARY DRAFT`
ont été vérifiés dans un navigateur.

La révision de code `d04c72c` a passé la
[matrice Python complète](https://github.com/Matt95354855/pcb_dev/actions/runs/38712518920)
et les [contrôles natifs](https://github.com/Matt95354855/pcb_dev/actions/runs/38712518850)
KiCad **9.0.9**, KiCad **10.0.2** et Ngspice. Pour le nouveau fixture, les deux
versions de KiCad confirment :

| Contrôle natif | Résultat |
| --- | --- |
| Chargement et géométrie via `pcbnew` | 6 pads, coordonnées, angles et perçages vérifiés |
| Netlist et parité modèle/schéma/PCB | Nets, références et types électriques conservés |
| Mapping volontairement incorrect | Rejeté comme attendu |
| ERC | Aucune violation, aucun contrôle ignoré |
| DRC | Exactement 3 connexions non routées, aucun contrôle ignoré ; statut bloqué conservé |
| Gerber, perçages, positions et SVG | Exports natifs produits ; aucune qualification de fabrication |

Les octets du PCB, du schéma et du projet présents dans l'archive publiée ont été
comparés aux hash des fichiers réellement testés dans les deux versions de KiCad :
ils sont identiques. Les preuves natives sont distinctes des tests unitaires et de
la base 0.1.1 ; les critères sont décrits dans le
[guide de génération](library-generation.md#smoke-natif-reproductible).

La qualification complète décrite dans [la feuille de route](roadmap.md) reste ouverte.

## Incrément 0.3.0 : interfaces physiques et NC explicites

La tranche ports/broches a passé la
[matrice Python](https://github.com/Matt95354855/pcb_dev/actions/runs/38742266293)
et les [tests natifs](https://github.com/Matt95354855/pcb_dev/actions/runs/38742266190)
au commit `b5662b9`. Les deux moteurs KiCad 9.0.9/10.0.2 confirment les quatre
ports réalisés en strict, la parité avec bibliothèques, le rejet d'une broche
déplacée sur le mauvais net et l'ERC sans violation ni contrôle ignoré.

Le smoke NC confirme les marqueurs de R1.2/R2.1 dans un export réel, puis rejette
un nouvel export réel sans marqueur. Les mutations de faux NC et de pad NC
raccordé sont également rejetées. Le DRC conserve trois connexions non routées
pour les cas nominal/interfaces et deux pour le cas NC ; aucun de ces résultats
ne vaut qualification de cuivre ou de fabrication.

Les régressions supplémentaires couvrent l'héritage de symbole avec NC et le
chaînage de la netlist exportée : un XML modifié après l'export natif bloque le
gate, en parité standard comme avec bindings. Les revues croisées n'ont trouvé
aucun autre faux succès concret dans ce périmètre.

Le paquet 0.3 a été installé dans un environnement neuf, le brouillon strict et
ses 34 fichiers ont été vérifiés. La visionneuse montre 4/4 interfaces réalisées
et conserve explicitement le statut de brouillon non fabricable.
