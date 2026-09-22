"""Constants for the Hydropolis Valbonne integration."""

from datetime import timedelta

DOMAIN = "hydropolis_valbonne"

CONF_CONTRAT_ID = "contrat_id"
CONF_COMPTEUR_NUMSERIE = "compteur_numserie"

DATA_REFRESH_INTERVAL = timedelta(hours=12)

OMEGA_SSO_URL = "https://omegasso.jvsonline.fr/api"
OMEGA_API_URL = "https://omegaweb.jvsonline.fr/api"
OMEGA_API_ID = "c49de86f84611ff40ef7b2af822a8614@iclient-hydropolis"

THREINT_API_URL = "https://api2.hydropolis-sophia.fr/api"
THREINT_API_ID = OMEGA_API_ID
