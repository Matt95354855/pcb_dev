# pcb_dev

**Concevoir un PCB comme un arbre de blocs, vérifier les interfaces avant le layout,
et conserver les preuves des contrôles.**

[![CI](https://github.com/Matt95354855/pcb_dev/actions/workflows/ci.yml/badge.svg)](https://github.com/Matt95354855/pcb_dev/actions/workflows/ci.yml)
[![Native KiCad](https://github.com/Matt95354855/pcb_dev/actions/workflows/native-kicad.yml/badge.svg)](https://github.com/Matt95354855/pcb_dev/actions/workflows/native-kicad.yml)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)
![Status alpha](https://img.shields.io/badge/status-alpha-orange)

[English](README.en.md) · [Tutoriel](docs/tutorial.md) · [Modèle](docs/model.md) ·
[Architecture](docs/architecture.md) · [Feuille de route](docs/roadmap.md) ·
[Contribuer](CONTRIBUTING.md)

`pcb_dev` est une couche de logique au-dessus de KiCad. Le projet contient un modèle
de blocs imbriqués, un moteur de vérification déterministe, une CLI et des adaptateurs
pour les outils EDA. Le moteur CAO reste KiCad.

**Version 0.1 alpha : les fichiers générés sont des aperçus fonctionnels.** Les symboles
et les pads génériques illustrent les connexions ; ils ne représentent pas des
composants physiques qualifiés. L'exemple MCU utilise un pinout de démonstration.
Aucune carte routée, validation électrique réelle ou autorisation de fabrication
n'est livrée par la commande `build`.

## Démarrage en deux minutes

Python **3.11 ou plus récent**, Git. Aucun paquet tiers au runtime.
KiCad est optionnel pour les vérifications du modèle et les aperçus.

```sh
git clone https://github.com/Matt95354855/pcb_dev.git
cd pcb_dev
python -m venv .venv
source .venv/bin/activate
# Windows PowerShell : .venv\Scripts\Activate.ps1
python -m pip install -e .

pcb-dev doctor
pcb-dev check examples/usb_mcu_led.json
pcb-dev build examples/usb_mcu_led.json --output build/demo
pcb-dev verify build/demo
```

Ouvrir `build/demo/index.html` dans un navigateur pour parcourir les blocs, leurs ports,
les composants et les résultats de vérification. L'aperçu est autonome, sans CDN ni
connexion réseau. Les fichiers KiCad se trouvent dans `build/demo/kicad/`.

Le répertoire de sortie doit être nouveau : une construction n'écrase jamais un
répertoire existant. Pour reconstruire, choisir par exemple `--output build/demo-v2`.

```sh
# Deux erreurs intentionnelles : codes de sortie 1
pcb-dev check examples/invalid_voltage.json
pcb-dev check examples/invalid_power_budget.json

# Dépendances et paramètres calculés
pcb-dev graph examples/usb_mcu_led.json --impact board.power
pcb-dev inspect examples/usb_mcu_led.json

# Geler un bloc dans un nouveau fichier
pcb-dev freeze examples/usb_mcu_led.json board.mcu --output build/frozen.json

# Archive vérifiée ; elle conserve son statut d'aperçu
pcb-dev archive build/demo --output build/demo-preview.zip
```

## Fonctionnalités présentes

| Domaine | Fonctionnement |
| --- | --- |
| Modèle | JSON strict, blocs imbriqués, ports typés, composants, empreintes déclarées, DNP et positions explicites |
| Dépendances | Tri topologique déterministe, cycles détectés, impact transitif d'un changement |
| Paramètres | Héritage, surcharge locale, équations arithmétiques sûres et contraintes booléennes |
| Amont | Compatibilité de tension au pire cas, type, polarité, logique, protocole, impédance et conflits de sorties |
| Puissance | Somme des charges par net transitif, capacité des sources et limites déclarées des composants |
| Ressources | Adresses I²C, dépendances de séquencement, délais déclarés, invalidation de blocs gelés |
| Traçabilité | Rapports JSON/Markdown, empreintes SHA-256, dérogations humaines liées au design exact |
| KiCad | Aperçus `.kicad_sch`, `.kicad_pcb`, projet, règles et netlist ; adaptateur natif ERC/DRC et exports |
| Fabrication | BOM générale/JLCPCB, CPL indicatif, profils de planification conservateurs, estimation largeur/chute de tension |
| Artifacts | Construction dans un répertoire temporaire, manifeste d'intégrité et ZIP reproductible |
| Revue | Visionneuse HTML locale par bloc et documentation du design générée |
| Paquets et bibliothèques | Versions exactes, hash, variantes DNP, inventaires de pins/pads et registre local |
| Simulation | SPICE explicite R/C/L/V/I, adaptateur NgSpice et explorations de tolérances reproductibles |
| Agent | Fournisseurs interchangeables, outils stricts, budgets, propositions atomiques, exigences protégées et traces masquées |

Les vérifications portent sur les **données déclarées**. Elles ne déduisent pas le
comportement électrique d'un composant, ne lisent pas automatiquement sa datasheet
et ne certifient pas son empreinte. Une expression calcule des paramètres ; elle ne
modifie pas implicitement un champ de port ou une valeur de composant.

## Simulation et agent

```sh
# Modèles passifs explicites ; aucun moteur SPICE n'est requis pour générer
pcb-dev check examples/rc_filter.json
pcb-dev spice examples/rc_filter.json --analysis tran --output build/rc.cir
# Exécution réelle seulement si NgSpice est installé
pcb-dev simulate build/rc.cir --output build/rc-simulation

# Rejouer un agent déterministe sans connexion ni clé, sur une copie du design
pcb-dev agent-demo examples/usb_mcu_led.json \
  --script examples/agent_session.json --output build/agent-review
```

L'agent écrit un `proposed-design.json` et une trace. Il n'altère pas le fichier
source et ne peut ni supprimer les exigences existantes, ni approuver une
dérogation, ni exporter une fabrication. Le transport configurable local/distant
est disponible avec `agent-run` ; aucun serveur de modèle n'est démarré par le projet.
Le fichier `examples/local-model.json` doit être adapté au modèle réellement servi.

[Blocs et paquets](docs/blocks.md) · [Bibliothèques KiCad](docs/libraries.md) ·
[Modèles SPICE](docs/spice.md) · [Harnais d'agent](docs/agent.md) ·
[Configuration des fournisseurs](docs/providers.md)

## Contrôles KiCad natifs

Installer KiCad **9 ou 10** et rendre `kicad-cli` accessible. Sur macOS, son emplacement
habituel est `/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli`.

```sh
pcb-dev doctor --require-kicad
pcb-dev native-check examples/usb_mcu_led.json \
  --board votre-carte.kicad_pcb \
  --schematic votre-schema.kicad_sch \
  --output build/native-review
```

Le backend conserve les rapports natifs, les versions d'outil et les hash des entrées.
Un outil absent, une version non supportée, une sortie vide, un rapport mal formé,
un contrôle ignoré ou une violation ne valent jamais validation. La commande combine
les preuves disponibles et garde la qualification de fabrication bloquée tant que
la cohérence du modèle et les autres contrôles requis ne sont pas prouvés.

La CI Python et la CI KiCad native sont distinctes. Les tests d'adaptateur utilisent
des doublures explicites ; ils ne constituent pas une exécution d'ERC/DRC. Voir
[les limites et commandes du backend](docs/backends.md).

## Ce qui reste avant le jalon fabricable

Le jalon cible de la todo list est une carte USB-C/3,3 V/MCU/LED avec des références
réelles, un schéma et des empreintes qualifiés, un routage, ERC/DRC à zéro et des
Gerber/BOM/CPL relus. Il n'est **pas encore atteint**.

Les priorités suivantes sont l'intégration des bibliothèques KiCad réelles au générateur, le lien contrôlé
entre pins et ports, la génération hiérarchique du schéma, le placement/routage,
les règles fabricant révisées et la relecture des exports. Les connecteurs fournisseurs,
l'extraction de datasheets, le GPU cloud et les modèles IA ne sont pas provisionnés.
Voir [l'état détaillé de chaque phase](docs/roadmap.md).

## Développement

```sh
python -m unittest discover -s tests -v
python -m pip install ruff==0.14.0
ruff check src tests
```

La CI vérifie Python 3.11/3.13/3.14 sur Linux, macOS et Windows, puis construit et
vérifie l'exemple. Les tests natifs KiCad sont définis séparément. Lire
[CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md) et
[les décisions d'architecture](docs/architecture.md) avant de modifier une porte
de validation.

Licence [MIT](LICENSE). Aucun service payant ni envoi de design n'est nécessaire
pour le parcours de démarrage.
