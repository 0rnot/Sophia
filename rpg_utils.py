# rpg_utils.py
import contextlib
import logging

logger = logging.getLogger('SophiaBot.RPGUtils')

@contextlib.asynccontextmanager
async def transaction(connection):
    logger.debug(f"Transaction started on connection: {connection}")
    await connection.execute("BEGIN")
    try:
        yield connection
        await connection.execute("COMMIT")
        logger.debug(f"Transaction committed on connection: {connection}")
    except Exception as e:
        logger.error(f"Transaction failed on connection {connection}, rolling back: {e}", exc_info=True)
        await connection.execute("ROLLBACK")
        logger.debug(f"Transaction rolled back on connection: {connection}")
        raise