import sys
sys.path.insert(0, "/app")
sys.path.insert(0, "/app/scripts")

from datetime import datetime, timezone, timedelta
import logging
import orchestrator

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("manual_matchmaking")

def main():
    log.info("Starting manual matchmaking and enrichment...")
    # Bypass window checks
    orchestrator.janela_matchmaking_aberta = lambda: (True, "")
    
    # We want to process works created/modified in the last 2 hours (since our run started at 10:17)
    snapshot_utc = datetime.now(timezone.utc) - timedelta(hours=2.5)
    
    log.info("Running matchmaking V2 (matches_v2)...")
    orchestrator.rodar_matchmaking_v2(dry_run=False)
    
    log.info("Running matchmaking V1...")
    orchestrator.rodar_matchmaking(snapshot_utc, dry_run=False)
    
    log.info("Running regenerar matches prestadores...")
    orchestrator.rodar_regenerar_matches_prestadores(dry_run=False)
    
    log.info("Running populador sintetico...")
    orchestrator.rodar_populador_sintetico(dry_run=False)
    
    log.info("Running wire-in decisores...")
    orchestrator.rodar_wire_in_decisores_empresa_alvo(dry_run=False)
    
    log.info("Running enrichment decisor top OURO...")
    orchestrator.rodar_enrichment_decisor_top_ouro(dry_run=False)
    
    log.info("Running intel commercial...")
    orchestrator.rodar_intel_obras_ouro(dry_run=False)
    
    log.info("Manual matchmaking and enrichment finished successfully!")

if __name__ == "__main__":
    main()
