# Parité des broches liées et des non-connexions explicites

`pcb_dev.bound_parity.check_bound_parity(bound, netlist, board)` compare un
`BoundDesign` chargé avec ses bibliothèques épinglées, une netlist XML KiCad et
les affectations des pads du PCB. Ce parcours est explicite et conserve le
comportement de `check_parity` et `parse_netlist` : ces deux fonctions continuent
de comparer tous les nets, sans ignorer les noms commençant par `unconnected-`.

```python
from pathlib import Path
from pcb_dev.bindings import load_bindings
from pcb_dev.bound_parity import check_bound_parity
from pcb_dev.model import load_design

design = load_design("examples/library_divider/design.json")
bound = load_bindings(design, "examples/library_divider/bindings.json")
report = check_bound_parity(bound, Path("native.net"), Path("board.kicad_pcb"))
```

La comparaison vérifie les références, valeurs, identifiants de symboles et
d'empreintes, numéros, noms et types électriques des broches, ainsi que leurs
nets connectés exacts. Les données résolues des bindings doivent correspondre
aux sources capturées et à leurs SHA-256. Les pads électriques attendus doivent
exister, avec leur multiplicité et leur type ; un pad mécanique à trou non
métallisé ne représente pas une broche électrique.

Une broche explicitement déclarée dans `no_connect_pins` est acceptée seulement
si la netlist exportée contient cette broche et le type réel de bibliothèque
suivi de `+no_connect`. Un type de bibliothèque intrinsèque `no_connect`, seul,
ne prouve pas la présence d'un marqueur de non-connexion. Les autres broches ne
peuvent recevoir ce suffixe. La convention du nom source `~` est comparée à son
encodage natif vide, sans modifier les noms réellement définis.

Les noms de `libparts` restent comparés exactement. Le champ `pinfunction` d'un
nœud possède deux encodages documentés : nom affiché dans KiCad 9, ou ce nom
suivi de `_` et du numéro physique dans KiCad 10. Seules ces deux chaînes
calculées depuis le nom source et le numéro exact sont admises ; aucun suffixe
libre n'est supprimé. Le second encodage est confirmé par l'[exporteur XML
officiel actuel](https://docs.kicad.org/doxygen/netlist__exporter__xml_8cpp_source.html)
et les artefacts réellement exportés par KiCad 10.0.2.

Le réseau XML d'une non-connexion doit contenir soit une seule broche, soit des
broches explicitement NC du même composant, empilées au même point source et
portant le même nom. Aucun réseau de ce type ne peut partager un net déclaré
connecté. Ces conditions suivent l'[exporteur XML officiel KiCad 9.0.9](https://raw.githubusercontent.com/KiCad/kicad-source-mirror/9.0.9/eeschema/netlist_exporters/netlist_exporter_xml.cpp),
notamment sa décision d'ajouter le suffixe après examen des broches empilées.
L'acceptation ne dépend jamais du préfixe du nom de net généré.

Dans cette version, chaque pad PCB NC doit rester sans net (`net 0` vide ou
absence de champ `net`), comme dans le générateur de brouillons liés. Une
affectation à un net de substitution NC sur le PCB reste refusée, même si KiCad
peut produire une telle représentation dans d'autres parcours d'édition.

Un rapport réussi contient les hashes du modèle, des bindings, du registre,
de la netlist et du PCB, ainsi que les broches et noms de réseaux NC réellement
observés. `native` reste `false` : appeler un comparateur ne lance pas KiCad.
La preuve d'export natif, son hash d'entrée correspondant au schéma contrôlé,
et les rapports ERC/DRC restent nécessaires. La parité des affectations ne
prouve pas l'absence de cuivre physique sur une broche NC, la conformité à une
datasheet, la géométrie complète, le routage ou la fabricabilité.

## Smoke réellement exécuté par KiCad

```sh
python -m pcb_dev.backends.smoke_no_connect --output build/native-no-connect
```

Le répertoire de sortie doit être nouveau et KiCad 9 ou 10 doit être disponible.
Le smoke copie le fixture de bibliothèques épinglées et transforme `R1.2` et
`R2.1` en deux non-connexions explicites. Le net `VOUT` disparaît entièrement ;
`VIN` et `GND` conservent chacun deux broches connectées. Les sources amont et
documents de licence restent présents ; seule cette variante de conception et
de bindings change.

Le smoke exige un véritable export des deux broches NC, une parité réussie,
un ERC sans exclusions réussi et un DRC conservant des connexions non routées
bloquantes. Il retire ensuite un marqueur dans une copie du schéma, réexporte
avec le moteur réel et exige le refus de la parité. Deux contre-exemples édités
(faux suffixe NC sur une broche connectée, pad NC affecté à `VIN`) doivent aussi
échouer ; leur résultat est distingué de l'export réellement natif.

Le rapport est enregistré dans `smoke-report.json`. L'absence de KiCad produit
un échec explicite, jamais un test passé ou sauté. Les tests unitaires utilisent
des mutations XML synthétiques et ne revendiquent aucune exécution native.
Une définition de workflow ou une réussite locale du comparateur ne prouve pas
que le smoke natif a été exécuté ; consulter les résultats réels du commit.
