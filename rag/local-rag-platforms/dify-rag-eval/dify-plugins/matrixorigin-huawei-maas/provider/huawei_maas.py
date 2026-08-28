from dify_plugin import ModelProvider
from dify_plugin.entities.model import ModelType
from dify_plugin.errors.model import CredentialsValidateFailedError


class HuaweiMaaSProvider(ModelProvider):
    def validate_provider_credentials(self, credentials: dict) -> None:
        try:
            model = self.get_model_instance(ModelType.TEXT_EMBEDDING)
            model.validate_credentials(model="bge-m3", credentials=credentials)
        except CredentialsValidateFailedError:
            raise
        except Exception as exc:
            raise CredentialsValidateFailedError(str(exc)) from exc
