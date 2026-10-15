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
