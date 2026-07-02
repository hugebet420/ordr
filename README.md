# Menu Scanner — V2

Application complète : photo de menu → page de commande en ligne avec paiement Stripe.

## Architecture

```
app.py                  ← Serveur Flask (routes client + admin + API)
menu_extractor.py       ← Extraction de menu (photo via Claude Vision / Google Places API)
static/style.css        ← Design glassmorphism Apple
templates/
  base.html             ← Template de base
  storefront.html       ← Page de commande client
  admin.html            ← Liste des commerces (admin)
  admin_new.html        ← Création de commerce (upload photo / recherche Google)
  admin_shop.html       ← Gestion d'un commerce (catalogue, commandes, lien public)
  confirmed.html        ← Confirmation de commande
  404.html              ← Page introuvable
data/                   ← Stockage JSON des catalogues et commandes
```

## Fonctionnalités

### Côté client (page de commande publique)
- Catalogue avec catégories et prix
- Panier avec stepper de quantité (+/-)
- Créneaux de retrait générés dynamiquement (exclut les heures passées)
- Saisie prénom + téléphone
- Paiement via Stripe Checkout
- Page de confirmation après paiement
- Responsive mobile, design glassmorphism

### Côté admin
- Création de commerce via upload photo OU recherche Google Places
- Extraction automatique du catalogue (produits, prix, catégories) par Claude Vision
- Modification/suppression de produits
- Re-upload de photo pour mettre à jour le catalogue
- Vue des commandes reçues
- Lien public copiable en un clic

### Fiabilité (menu_extractor.py)
- Retry automatique (2 tentatives) sur erreur API
- Validation stricte du JSON retourné
- Détection des prix aberrants (négatifs ou > 500€)
- Indicateur de confiance (haute/moyenne/basse)
- Avertissements visibles si extraction partielle
- Validation de format et taille du fichier uploadé

## Lancement

```bash
# Dépendances
pip install flask stripe --break-system-packages

# Clés API (voir section "Comment obtenir les clés" ci-dessous)
export ANTHROPIC_API_KEY="sk-ant-..."
export STRIPE_SECRET_KEY="sk_test_..."
export STRIPE_PUBLISHABLE_KEY="pk_test_..."
export GOOGLE_PLACES_API_KEY="..."  # optionnel

# Lancer
python3 app.py

# Accès
# Client : http://localhost:5000/shop/<id>
# Admin  : http://localhost:5000/admin
```

## Comment obtenir les clés

### Stripe (obligatoire pour le paiement)
1. Crée un compte gratuit sur https://stripe.com
2. Dashboard → Developers → API Keys
3. Copie "Secret key" (sk_test_...) et "Publishable key" (pk_test_...)
4. Pour tester les paiements : utilise la carte fictive 4242 4242 4242 4242, date future quelconque, CVC quelconque
5. Aucun vrai argent ne sera débité tant que tu utilises les clés test

### Anthropic (obligatoire pour l'extraction photo)
Tu as déjà ta clé — c'est la même que celle que tu utilises pour Claude Code.

### Google Places API (optionnel)
1. https://console.cloud.google.com → Créer un projet
2. API Library → chercher "Places API (New)" → Activer
3. Credentials → Create API Key
4. Le tier gratuit (200$/mois de crédits) suffit largement pour démarrer

## Modèle économique intégré

- **Commission plateforme : 10% + 0,30€ fixe par commande**, prélevée automatiquement via Stripe Connect (frais de destination).
- L'argent du client va directement sur le compte Stripe du commerçant — toi tu ne touches jamais les fonds, seule ta commission est transférée vers ton compte plateforme.
- Les frais Stripe (carte bancaire, ~1,4% + 0,25€) sont déduits de TA commission, pas de l'argent du commerçant.
- Avant de pouvoir recevoir des commandes, chaque commerçant doit faire l'onboarding Stripe (bouton "Activer les paiements" dans `/admin/shop/<id>`) — Stripe vérifie son identité et ses coordonnées bancaires (~5-10 min, formulaire géré entièrement par Stripe, pas par toi).

## Ce qui reste à faire (avec Claude Code sur ta machine)

- Déploiement en ligne (ton pipeline Cloudflare existant, ou un VPS)
- Notifications au commerçant quand une commande arrive (email ou SMS)
- Webhook Stripe pour confirmer le paiement côté serveur de façon fiable (actuellement la confirmation se fait via la page de retour, ce qui est moins robuste qu'un webhook si le client ferme l'onglet avant la redirection)
- Authentification admin (actuellement /admin est accessible sans login — n'importe qui connaissant l'URL peut voir tous les commerces)
- Historique des modifications de catalogue
