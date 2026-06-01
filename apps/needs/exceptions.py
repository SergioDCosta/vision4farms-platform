from django.core.exceptions import ValidationError


class DuplicateActiveNeedError(ValidationError):
    def __init__(self, existing_need):
        self.existing_need = existing_need
        super().__init__(
            "Já existe uma necessidade ativa para este produto. "
            "Edite essa necessidade em vez de criar outra."
        )
