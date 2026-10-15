# Bibliothèques KiCad : inventaire et intégrité

`pcb_dev.libraries` lit des fichiers **réels** `.kicad_mod` et `.kicad_sym` au
format S-expression moderne de KiCad. Il produit un inventaire structuré,
quelques contrôles géométriques bornés et un registre local avec SHA-256. Il ne
génère pas d'empreinte IPC, ne certifie pas une bibliothèque et ne modifie pas
la configuration globale de KiCad.

## Lire une empreinte

```python
from pcb_dev.libraries import load_footprint, check_footprint

footprint = load_footprint("libraries/MyParts.pretty/SOIC-8.kicad_mod")
print(footprint.to_dict())
report = check_footprint(footprint, silk_clearance_mm=0.05)
print(report.to_markdown())
```

Chaque pad fournit son numéro, type, forme, couches, position, rotation, largeur,
hauteur et dimensions/offset du perçage s'il existe. Plusieurs pads peuvent
partager un numéro, comme dans certaines empreintes thermiques. Les chemins de
modèles 3D sont conservés comme **chaînes inertes** : aucune variable
d'environnement n'est développée, aucun fichier STEP n'est ouvert, et aucune
URL n'est téléchargée.

Contrôles disponibles :

| Contrôle | Portée et limite |
| --- | --- |
| Dimensions de pad positives | Erreur si une dimension est nulle ou négative. |
| Perçage déclaré | Dimensions positives pour les pads traversants ; revue de la marge rectangulaire tenant compte de l'offset. Aucun calcul IPC d'anneau minimal. |
| Repère de broche 1 | Avertissement en l'absence d'un pad `1` ou `A1`. Les conventions propres au composant doivent être revues. |
| Courtyard | Présence de géométrie sur `F.CrtYd` ou `B.CrtYd` uniquement. Fermeture, espacement, enveloppe et dégagement restent à vérifier dans KiCad. |
| Sérigraphie/pad | Approximation des traits `fp_line`, `fp_rect` et contours `fp_poly` contre les rectangles englobants orientés des pads. La largeur de trait et la marge demandée sont prises en compte. |

Les traits sont comparés à la face cuivre correspondante. La transformation des
pads tient compte des rotations et de l'axe Y des coordonnées PCB. Les formes
circulaires, ovales et à coins arrondis utilisent leur rectangle englobant :
des faux positifs restent possibles près des coins. Un recouvrement produit
donc un avertissement à inspecter, pas une conclusion de non-conformité.

Les arcs, cercles, courbes, textes, intérieurs remplis, pads custom/trapézoïdaux
et formes inconnues sont signalés comme non vérifiés. Les ouvertures réelles de
masque, les dégagements fabricant, les formes de cuivre exactes et les modèles
3D sont hors périmètre. Un rapport sans erreur ne signifie jamais « fabricable » :
son champ `manufacturing_qualified` reste `false` et ce rapport n'est pas une
preuve native ERC/DRC.

## Lire des symboles et vérifier les numéros de broches

```python
from pcb_dev.libraries import load_symbol_library, check_symbol_footprint

library = load_symbol_library("libraries/MyParts.kicad_sym")
symbol = next(item for item in library.symbols if item.name == "MySensor")
for pin in symbol.pins:
    print(pin.number, pin.name, pin.electrical_type, pin.unit, pin.convert)
print(check_symbol_footprint(symbol, footprint).to_markdown())
```

L'inventaire conserve numéro, nom, type électrique, forme, position, orientation,
longueur, unité, variante de représentation et visibilité des broches. Les
propriétés du symbole sont conservées. Les symboles dérivés par `extends`
héritent des broches et propriétés d'un parent présent dans le même fichier.
Les cycles et parents absents sont refusés. Un symbole dérivé qui déclare aussi
des broches est refusé, car les règles de surcharge ne sont pas implémentées.

`check_symbol_footprint` compare les **ensembles de numéros** : chaque broche doit
avoir un pad électrique et chaque pad électrique doit avoir une broche. Les
trous non métallisés sont exclus. Les répétitions d'un même numéro, notamment
entre représentations d'un symbole, n'ajoutent pas de broche physique. Les noms,
types, fonctions alternatives et correspondances au composant réel exigent
toujours une lecture de la datasheet. Le rapport conserve
`datasheet_pinout_verified=false`.

## Registre local épinglé

```python
from pcb_dev.libraries import register_library, load_pinned_library

register_library(
    "libraries/MyParts.pretty/SOIC-8.kicad_mod",
    "libraries.lock.json",
    name="sensor-footprint",
)
register_library(
    "libraries/MyParts.kicad_sym",
    "libraries.lock.json",
    name="sensor-symbols",
)
verified = load_pinned_library("libraries.lock.json", "sensor-footprint")
```

Le fichier doit se trouver à l'intérieur du répertoire qui contient le registre.
Les entrées utilisent un alias unique, un type (`footprint` ou `symbols`), un
chemin relatif et le SHA-256 des octets du fichier. Le fichier est parsé avant
enregistrement. Un nouvel enregistrement sous le même alias remplace son
ancienne empreinte ; cette modification doit être revue dans Git.

Les chemins absolus, traversées `..`, chemins Windows ambigus et liens
symboliques sortant de ce répertoire sont refusés. Le chargement vérifie le
hash **avant** de parser les mêmes octets. Toute modification, même un ajout de
fin de ligne, invalide le pin. Aucun script, import Python, archive, installateur
ou appel réseau n'est exécuté. Les fichiers sont bornés à 32 Mio, le parseur
S-expression borne aussi les tokens et la profondeur. Les doublons de champs
structurants et nombres non finis sont refusés.

Le registre protège l'intégrité des fichiers acceptés, pas leur provenance
électronique. Conserver les bibliothèques et le registre dans Git avec leurs
licences et sources. Les empreintes globales KiCad ou références de catalogues
restent de simples identifiants tant que ces fichiers réels n'ont pas été
qualifiés dans le projet.

Références de format : [introduction S-expression officielle](https://dev-docs.kicad.org/en/file-formats/sexpr-intro/),
[format des bibliothèques d'empreintes](https://dev-docs.kicad.org/en/file-formats/sexpr-footprint/),
[format des bibliothèques de symboles](https://dev-docs.kicad.org/en/file-formats/sexpr-symbol-lib/).
