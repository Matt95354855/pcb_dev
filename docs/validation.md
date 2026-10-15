# Validation de la livraison 0.1.0

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

Le smoke KiCad attend des violations de connexions non routées sur l'aperçu.
Son succès signifie que les fichiers se chargent, que les nets sont conservés
et que les constats natifs sont correctement retenus. Il ne signifie pas que
la carte passe DRC ni qu'elle est fabricable.

La qualification complète décrite dans [la feuille de route](roadmap.md) reste ouverte.
