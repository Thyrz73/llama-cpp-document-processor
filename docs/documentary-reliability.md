# Fiabilité documentaire — audit et compte rendu d’implémentation

> Ce rapport décrit le code au moment de l’audit. Les emplacements cités (`app.py`, `run`, `generate`…) ont été réorganisés depuis : voir l’[architecture actuelle](architecture.md).

Validation locale du 2 novembre 2026. Aucun déploiement, appel à un modèle réel, changement de serveur, commit ou push.

## Diagnostic de l’existant

L’audit a précédé les modifications. Le pipeline V2 est déjà séquentiel, borné en contexte et reprenable ; il n’a pas été reconstruit.

| Élément audité | Implantation et constat avant modification |
| --- | --- |
| Point d’entrée | `app.py` : Flask/Waitress, `worker_loop`, `analyze_job` → `pipeline.run`. L’import ne démarre pas le serveur. |
| Extraction | `extract_pdf` : PyMuPDF, repli pypdf, OCR sélectif Tesseract. Numéros PDF physiques conservés ; texte partiel conservé et signalé. |
| Découpage | `split_with_page`, `make_chunks` : passages bornés et tokenisés, marqueurs ajoutés par passage, liste des pages de chaque section. |
| Marqueurs | `[Page N]` provient de l’extraction, mais des références peuvent aussi apparaître dans le texte non fiable du PDF. |
| Prompts | `prompts.TEMPLATES` et `instruction` ; copie immuable par tâche. Plusieurs distinctions scientifiques demandées existaient déjà. |
| Appel llama.cpp | `ModelClient.complete` → `generate`, messages indépendants et plafond de contexte vérifié avec le tokenizer du moteur. |
| Streaming | SSE UTF-8, délai maximal, annulation, verrou évitant deux flux simultanés ; sortie tronquée refusée. |
| Raisonnement/réponse | `ThinkingSplitter` et canal séparé du moteur ; raisonnement exclu des notes et du rapport. |
| Notes | Markdown brut, artefacts vérifiés par empreintes. Pas de registre numérique indépendant des générations. |
| Fusions | `plan_groups`, groupes ordonnés de quatre au maximum, singleton conservé, garde contre les boucles. Seul le texte généré était transmis. |
| Rapport | Dernière génération puis en-tête localisé et publication atomique. Le prompt imposait des rubriques et une réponse directe, même redondantes. |
| Sauvegarde/reprise | SQLite, snapshots privés, manifestes et dépendances, archivage des anciens rapports. Fonctionnalités conservées. |
| Profils | `profiles.py` : Low/Medium/High/Ultra ; `config.resolve` conserve aussi le mode historique et les ajustements de détail. |
| Vérifications | Comptages de différences entre ensembles de chaînes numériques et de références. Ni association entité/valeur, ni avertissement publié pour une page impossible. |
| Tests | 66 tests Python existants, tests navigateur Node, plan de benchmark hors ligne. Deux tests exigent Tesseract réel. |

**Causes établies dans le code :** une valeur présente dans le texte mais attribuée à une autre entité échappait aux compteurs ; les réserves et références omises par une génération n’étaient pas récupérées depuis une provenance indépendante ; les alertes mécaniques n’étaient pas publiées dans le rapport.

**Causes plausibles, non démontrées :** taille du modèle, ordre d’extraction des tableaux, compression successive et longueur des consignes peuvent contribuer aux erreurs. Sans exécution comparative de Qwen et sans rapports historiques complets, on ne peut pas attribuer une erreur précise à l’un de ces facteurs.

## Modifications et fonctionnement

| Fichier / fonctions | Changement et effet | Coût supplémentaire |
| --- | --- | --- |
| `app.py` / `make_chunks` | Ajout de `parts`, passages exacts et pages physiques, indépendamment des marqueurs contenus dans le texte. Texte envoyé au modèle inchangé à cette étape. | Une copie des passages dans les données de découpage et sur disque. |
| `reliability.py` / `build_evidence`, `extract_comparisons` | Registre indépendant des réponses : lignes numériques reconnues, contexte, périodes, unités, pages, identifiants, réserves lexicales et métadonnées OCR/texte insuffisant. | Un parcours supplémentaire du texte déjà extrait, sans nouvelle lecture du PDF. Bibliothèque standard uniquement. |
| `reliability.py` / `decimal_value`, `derived`, `numeric_checks` | Variations signées et pourcentages en `Decimal`, tolérance liée à l’arrondi affiché. Recherche de discordances sur une entité unique. | Calculs locaux, aucun modèle ni tokenizer. |
| `reliability.py` / `citation_checks`, `quote_checks` | Existence des pages, appartenance aux entrées, disponibilité du texte ; contrôle limité de citations textuelles exactes présentes dans le registre. | Parcours des réponses et des éléments structurés. Pas de recherche sémantique. |
| `reliability.py` / `note`, `merge_note`, `combined`, `prepare_input` | Pages et sections conservées hors Markdown, alertes transmises, bloc de preuves borné et dédupliqué par élément source. Repli compact si le contexte est trop serré. | Entrées de fusion/rapport plus longues ; une seconde vérification de contexte seulement si le bloc complet ne tient pas. |
| `reliability.py` / `certainty_checks`, `report_warnings` | Signaux lexicaux prudents pour certaines causalités affirmatives et l’étalonnage « non mentionné » devenu « non réalisé ». Avertissements FR/EN ajoutés au rapport ; aucun remplacement de données. | Quelques expressions régulières ; texte supplémentaire seulement en cas d’alerte. |
| `pipeline.py` / `run`, `generate`, `check_step` | Intégration des contrôles, conservation du registre et des diagnostics privés, recomposition des métadonnées à la reprise. Compteurs historiques conservés ; nouveaux compteurs publics sans valeurs sources. | Écritures JSON locales et contrôles des sorties, y compris des sorties réutilisées. |
| `prompts.py` / `TEMPLATES`, version 4 | Association entité/variable/période/statut, conservation du caractère fictif et des hypothèses, réserves applicables aux conclusions ; rubriques adaptées au document. | Consignes d’étape légèrement plus longues. Prompt système et budgets inchangés. |
| `tests/test_reliability.py`, `README.md`, `docs/api.md`, ce document | Tests et documentation des comportements, limites, mesures et validation manuelle. | Aucun coût d’exécution applicatif. |

### Données numériques : périmètre volontairement limité

Formats reconnus :

```text
A : 12,4 → 10,8 MWh
Bâtiment | Janvier–mars 2025 | Janvier–mars 2026 | Variation
A · École | 12,4 MWh | 10,8 MWh | −12,9 %
```

Les tableaux doivent comporter un en-tête avec deux années distinctes et des lignes complètes contenant deux valeurs de même unité. Les colonnes séparées par `|` ou plusieurs espaces sont acceptées. Les unités doivent être explicitement présentes dans les cellules ; la liste reconnue est définie dans `UNIT`. Un tableau aux unités seulement dans l’en-tête, multiligne, incomplet ou mal reconnu par OCR reste hors du contrôle automatique.

Chaque ligne conserve entité, variable/contexte textuel, périodes disponibles, unité, page, texte source et statut. `source_unspecified` évite d’inventer un statut « mesuré » ; `calculated` désigne les variations recalculées. Une flèche sans périodes établit seulement un ordre de calcul, pas la comparabilité scientifique des mesures. Les conditions absentes restent inconnues.

Les virgules et points sont traités comme séparateurs décimaux ; les milliers peuvent être regroupés par espaces ordinaires, insécables ou fines insécables. Les formats mélangeant point et virgule sont refusés. **La ponctuation de milliers anglo-saxonne n’est pas interprétée** : un document utilisant `1,234` pour mille deux cent trente-quatre reste ambigu et demande une lecture humaine. Aucune substitution OCR, conversion d’unité ou réparation de cellule n’est effectuée.

Une discordance de rapport est recherchée seulement si l’entité est unique dans les entrées pertinentes, les unités sont compatibles et la formulation indique une variation ou un résumé pourcentage/variation signés. Une valeur absolue signée isolée peut être une mesure : elle n’est pas traitée automatiquement comme une variation. Une autre période explicite, plusieurs lignes pour une même entité, des valeurs avant/après multiples ou une unité ambiguë font abandonner le contrôle. Les pourcentages et points de pourcentage ne sont pas confondus : les comparaisons dont l’unité source est `%` restent non contrôlées.

Sur la fixture et le PDF de référence, les calculs donnent A = −1,6 MWh, B = +0,6 MWh, C = −1,6 MWh, total = −2,6 MWh, variation globale ≈ −7,24 %. La sortie `École (A) : −12,9 % (−2,6 MWh)` produit une alerte ciblée ; elle reste visible avec son avertissement.

`compare_quantities` fournit aussi un contrôle pour des quantités **déjà structurées** avec même entité, variable, période, unité et conditions. Prévision 3,0 / observation 2,6 donne un écart de −0,4, jamais automatiquement une contradiction. Des observations discordantes donnent une discordance numérique à examiner. Cette fonction n’infère pas ces champs depuis une prose libre. Les passages de prévision/observation repérés lexicalement sont conservés comme extraits sources.

Les sommes de tableaux ne sont pas certifiées : l’extraction actuelle ne garantit pas que les lignes constituent exactement le périmètre d’un total. Les incohérences de calcul d’une prose libre, les conversions d’unités et les associations sémantiques complexes restent à examiner manuellement. Les diagnostics indiquent `not_performed` lorsque rien n’a pu être contrôlé, ou `partial`, avec le nombre de contrôles réellement effectués.

### Pages, réserves et fusions

Le domaine des pages est `1..page_count`, issu du lecteur PDF, pas des numéros imprimés ou des citations présentes dans un extrait. Les listes, références multiples et plages sont acceptées, y compris avec tirets Unicode ; les plages immenses sont contrôlées sans les développer en mémoire. Une page sans texte ou insuffisante après OCR existe néanmoins : elle déclenche un avertissement distinct.

**Une page existante ne prouve jamais que l’affirmation est étayée.** Le contrôle supplémentaire des citations textuelles se limite aux extraits du registre, normalisés pour la casse, les espaces et la ponctuation. Il signale une attribution douteuse ; il ne retrouve pas toutes les paraphrases et ne constitue pas une validation sémantique exhaustive.

Lorsqu’une ligne contient plusieurs références, le contrôle s’abstient si la page source de la citation figure parmi elles. Les autres références peuvent concerner d’autres affirmations de la même ligne. Une citation retrouvée sur une seule page source disponible reste signalée lorsque cette page est absente de toutes les références de la ligne.

Les enveloppes de notes conservent toutes leurs pages et sections d’origine, même si le modèle ne cite plus rien. Elles transmettent également les codes d’alertes antérieurs, éventuellement déjà résolus. Les réserves restent dans le registre source, jamais remplacées par les interprétations du modèle. Les mots-clés identifient des **candidats**, pas des faits scientifiquement qualifiés.

Le bloc source ajouté aux fusions et au rapport est plafonné à **2 400 caractères**, compte d’omissions compris. Les lignes numériques disposent d’une première part bornée ; les réserves favorisent lexicalement les précautions causales et les hypothèses nommées. Le reste de l’espace est réutilisé. Les doublons purement fictifs ne sont pas tous répétés. Les extraits trop longs ne sont pas coupés en citations incomplètes : ils restent dans le registre, avec les autres extraits omis. Cette sélection ne garantit pas de choisir toutes les informations essentielles sur un long document.

Si le contexte complet ne tient pas, une vérification supplémentaire tente les mêmes notes et métadonnées sans ce bloc, avec mention explicite de l’omission. Cela évite une fusion ajoutée uniquement pour loger les preuves. Le plafond choisi (`evidence_prompt_limit`) est enregistré. Si les notes elles-mêmes ne tiennent pas, les garde-fous de fusion/contextes existants s’appliquent. Le registre complet reste disponible localement ; il n’est pas possible de promettre que toutes ses réserves apparaîtront dans le rapport généré.

### Sauvegarde, reprise et confidentialité

Dans `data/jobs/<id>/artifacts/<configuration-hash>/` :

- `chunks.json` inclut les passages/pages dans `parts` ;
- `source_evidence.json` conserve les éléments reconnus, les réserves et leur provenance ; il est vérifié par le manifeste et ses dépendances ;
- `quality_checks.json` conserve le périmètre pages/sections de chaque sortie, les alertes détaillées et les contrôles non effectués. C’est un diagnostic recalculable, pas une autorité utilisée pour modifier le rapport ;
- les notes, fusions et réponses finales restent des fichiers Markdown bruts. Le rapport publié à l’emplacement historique ajoute les avertissements, sans modifier la réponse archivée du modèle.

Le registre est reconstruit si son empreinte est invalide. Les diagnostics sont recalculés même pour les générations réutilisées. Le versionnement des dépendances invalide les anciens artefacts de génération lors d’une reprise avec ce code ; prévoir donc une possible réanalyse des anciennes tâches. Les snapshots de prompts restent immuables : **créer une nouvelle tâche pour bénéficier des prompts version 4**. Les anciens rapports sont archivés avant remplacement, comme auparavant.

Aucune migration SQLite ni nouvelle dépendance. Ne pas éditer manuellement les artefacts. Le registre et les diagnostics contiennent des données documentaires privées et doivent être sauvegardés/protégés comme les notes existantes. Les logs et métriques API ne reçoivent que des compteurs et codes ; aucun nouveau endpoint ne publie ces fichiers. Les textes extraits, y compris leurs éventuelles instructions malveillantes, ne modifient ni paramètres, ni système, ni règles de vérification. Les protections de génération existantes restent nécessaires.

## Tests et compatibilité

Suite initiale : **66 tests, 64 réussites, 2 erreurs** dues à l’absence de Tesseract. Suite après modification : **88 tests, 86 réussites, les mêmes 2 erreurs d’environnement**. Les 22 nouveaux tests passent. Les deux tests concernés sont `test_image_pdf_uses_ocr` et `test_mixed_pdf_ocr_only_scan_page` dans `test_ocr.TestExtraction` ; ils n’ont pas été désactivés ni modifiés.

| Critère demandé | Vérification effectuée |
| --- | --- |
| A — Pages impossibles | Pages 1/3 existantes, page 4 signalée ; aucune validation sémantique déclarée. |
| B — Entités/chiffres | Quatre variations, pourcentage global, attribution erronée à A et signe erroné pour B. |
| C — Prévision/observation | Écart de 0,4 avec contexte explicitement comparable, statut de comparaison et refus des périmètres différents. |
| D/E — Prudence | Réserves conservées malgré trois omissions simulées ; alertes lexicales FR/EN et consignes de conclusion. Ne teste pas la compréhension du modèle. |
| F — Provenance | Listes/plages, références multiples, pages non consécutives, page vide, OCR simulé sur PDF réel, référence trompeuse déjà présente dans le texte, citation exacte sur mauvaise page. |
| G — Unicode | Chaîne demandée, accents, minus Unicode, espaces insécables, conservation JSON/Markdown ; anciens tests UTF-8 intégralement conservés. |
| H — Fusions | Sources chiffrées proches, hypothèses/réserves, écart prévision/observation, métadonnées indépendantes ; 20 sections, deux niveaux de fusion, reprise. |
| I — Profils | Même sortie erronée contrôlée avec Low, Medium, High et Ultra ; mêmes critères, budgets du profil conservés. |
| J — Robustesse | Données OCR incorrectes, cellules/unités manquantes, ambiguïtés, zéro initial, arrondis, périodes différentes ; notes Markdown sans schéma imposé. Tests de flux tronqués/malformés existants conservés. |

Les tests d’intégration utilisent les vraies routes Flask, SQLite, manifestes, sauvegardes, reprise et téléchargement UTF-8 avec modèle simulé et réseau interdit. Sur 20 sections : 20 analyses + 5 fusions au premier niveau + 1 au second + 1 rapport = **27 appels** ; après échec du rapport, **1 seul** appel à la reprise ; après réussite, **0 génération** pour rejouer les artefacts vérifiés. Les tests existants à huit sections conservent leurs **11 appels**.

Le PDF local `PDF_TEST_ANALYSE_FR_DOCUMENT_PROCESSOR.pdf` est présent mais ignoré par Git. Le test associé utilise réellement son extraction et son découpage : 3 pages, 1 section, 4 lignes reconnues, 2 générations simulées. S’il manque dans un clone, seul ce test local est ignoré ; les fixtures textuelles incluses dans les tests restent autonomes.

Le suivi en direct, le raisonnement séparé, les téléchargements, les langues explicites FR/EN et Unicode passent leurs tests Python. Les styles/thèmes, Docker, llama.cpp et les profils n’ont pas été modifiés. Le mode `auto` continue de suivre la question pour la génération ; comme les métadonnées historiques, les nouveaux avertissements utilisent l’anglais lorsque la langue n’est pas explicitement française.

Syntaxe Python et plan de benchmark hors ligne : réussis. Tests navigateur et syntaxe JavaScript non exécutés : **Node.js absent**. Construction Docker et validation Compose non exécutées : **Docker absent**. OCR réel non validé ici ; extraction/OCR simulé et transport de sa provenance validés. Aucun test réel Qwen ni benchmark d’inférence long n’a été lancé.

## Performance mesurée et limites des mesures

Micro-mesure locale sous Python 3.14.4 : construction du registre depuis le texte déjà extrait, fabrication du bloc source, contrôle d’une courte réponse, médiane de sept exécutions. Le jeu de 120 pages répète les trois pages de référence avec de nouvelles pages physiques ; ce n’est pas une mesure complète d’analyse d’un PDF long. Pic d’allocations Python mesuré séparément avec `tracemalloc`, hors extraction et hors moteur.

| Jeu | Temps médian approximatif | Pic Python supplémentaire approximatif | Registre JSON UTF-8 | Bloc ajouté au prompt |
| --- | ---: | ---: | ---: | ---: |
| PDF de référence, 3 pages | 3,2 ms | 29 Kio | 7,9 Ko | 2 322 caractères |
| Fixture textuelle, 120 pages | 118 ms | 830 Kio | 312 Ko | 2 327 caractères |

Ces mesures ne concernent pas le CPU i5-4210H de référence, le RSS total ni la durée d’inférence. Elles ne prouvent aucun gain de vitesse.

- Aucun appel LLM de vérification, ni continuation automatique.
- Aucune tokenisation dans le module de fiabilité. Les vérifications de contexte normales sont conservées ; une nouvelle tokenisation est possible uniquement pour le repli compact en cas de contexte serré.
- Un parcours supplémentaire du texte extrait ; aucune relecture supplémentaire du PDF pour les contrôles.
- Calculs `Decimal`, analyses de chaînes et écritures de diagnostics supplémentaires. Le registre et les passages de découpage augmentent la mémoire et le stockage proportionnellement au document ; aucune copie de modèle, image OCR ou embedding supplémentaire.
- Prompts d’étape : sections 1 321 → 1 581 caractères, fusions 836 → 1 117, final 1 084 → 1 557. Le système reste inchangé. Le bloc source s’ajoute seulement aux entrées de fusion/final, pas à chaque section.
- Les générations peuvent coûter davantage en prétraitement à cause des entrées plus longues. Ni tokens réellement utilisés, ni temps Qwen, ni mémoire du moteur, ni qualité finale réelle n’ont été mesurés ici. Aux limites du contexte, le plan de fusion peut toujours varier en raison des métadonnées/consignes ; le repli compact limite l’impact des preuves.
- Budgets Low inchangés : `3000 / 3072 / 2048 / 3072`. Les trois autres profils, le mode historique, le multiplicateur de détail, la fenêtre de contexte et les paramètres d’inférence restent inchangés.

## Benchmark qualité à effectuer manuellement

Les tests ne donnent **aucune nouvelle note sur 20**. La référence humaine de 14/20 reste la dernière évaluation réelle disponible. La cible de 17/20 n’est pas démontrée.

Pour comparer une nouvelle tâche Low sur le PDF de référence, conserver la question, le modèle, les budgets, les réglages moteur et les conditions de cache ; relever les métriques existantes (tokens d’entrée/sortie, prefill/decode, appels, temps total, mémoire et erreurs). Examiner le PDF en parallèle du rapport, y compris les avertissements et les omissions.

| Critère humain | Maximum | Points à vérifier |
| --- | ---: | --- |
| Exactitude des chiffres | 5 | A/B/C/total distincts, signes, MWh/kWh, −7,24 %, prévision 3,0 vs observation 2,6 et écart 0,4. |
| Méthodes/hypothèses | 3 | H1/H2 non vérifiées, période janvier–mars et conditions de comparaison. |
| Prudence scientifique | 4 | Causalité non démontrée, météo/occupation non isolées, étalonnage non mentionné. |
| Références de page | 3 | Existence et soutien réel des affirmations examinés séparément. |
| Exhaustivité/fidélité | 3 | Caractère fictif, données absentes, référence externe non vérifiée, discordance de la note à 10 % avec réserve de périmètre. |
| Structure/lisibilité | 2 | Rubriques pertinentes, réserves cohérentes avec la conclusion, absence de répétitions inutiles. |

Justifier les pénalités et conserver les rapports pour comparaison. Aucun score scientifique ne peut être établi par les seuls tests de chaînes ou compteurs.

## Validation locale et futur déploiement manuel

```bash
.venv/bin/python -m unittest discover -s tests -p test_reliability.py -v
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q app.py document_processor scripts tests
.venv/bin/python scripts/benchmark.py --variants docs/benchmark-variants.example.json --repeat 1
node tests/test_polling.js
for file in static/js/*.js; do node --check "$file"; done
git diff --check
```

Installer les prérequis de développement décrits dans le README pour exécuter Tesseract réel et Node. Le plan de benchmark ci-dessus n’effectue aucun appel réseau. La suite ordinaire n’a pas besoin de llama.cpp. Un essai réel du modèle constitue une validation optionnelle distincte.

Fichiers applicatifs/documentaires modifiés : `app.py`, `document_processor/pipeline.py`, `document_processor/prompts.py`, `README.md`, `docs/api.md`. Fichiers ajoutés : `document_processor/reliability.py`, `tests/test_reliability.py`, `docs/documentary-reliability.md`. La modification de `.gitignore` était déjà présente à l’ouverture de la tâche et n’a pas été retouchée. `git diff --stat` ne compte pas les nouveaux fichiers non suivis : consulter aussi `git status --short` et leur contenu.

État de `git diff --stat` à la livraison, incluant la ligne préexistante de `.gitignore` :

```text
 .gitignore                     |  1 +
 README.md                      |  2 +
 app.py                         | 11 +++--
 docs/api.md                    |  2 +
 document_processor/pipeline.py | 91 +++++++++++++++++++++++++++++++-----------
 document_processor/prompts.py  | 23 ++++++++---
 6 files changed, 98 insertions(+), 32 deletions(-)
```

S’y ajoutent les trois nouveaux fichiers non suivis : module de fiabilité (498 lignes), tests (390 lignes) et présent compte rendu. Rien n’a été ajouté à l’index Git.

Pour un futur déploiement, examiner ces changements, exécuter les contrôles dans l’environnement cible Python 3.12/Tesseract/Node/Docker, puis suivre la procédure de sauvegarde/mise à jour manuelle existante dans le README et `docs/migration-v2.md`. Préserver `.env`, le prompt réel et les données ; aucune nouvelle variable ni migration de base n’est nécessaire. Prévoir les réanalyses éventuelles à la reprise, créer une nouvelle tâche Low, vérifier le téléchargement et comparer le rapport au PDF avant généralisation. Aucune de ces opérations de production n’a été exécutée.

## Limites restantes

Le petit modèle peut encore omettre une réserve, attribuer un chiffre à une mauvaise variable, ignorer une consigne ou produire une conclusion scientifiquement erronée. Les contrôles ne reconstruisent pas tous les tableaux, ne valident pas toutes les références et ne détectent pas toutes les causalités ou contradictions. Les alertes lexicales ont des faux positifs et faux négatifs ; elles ne tiennent pas lieu d’analyse scientifique. Une absence d’alerte, un calcul textuellement cohérent ou une page existante ne prouvent pas la vérité d’un rapport. Le registre rend une revue plus traçable ; il ne remplace pas cette revue.
