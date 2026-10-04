class StageError(Exception):
    """Falha de uma etapa; `status` vai para meta.json e para o funil de status."""

    def __init__(self, status: str, detail: str = ""):
        super().__init__(f"{status}: {detail}")
        self.status, self.detail = status, detail
