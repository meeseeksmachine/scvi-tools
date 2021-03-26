import torch
from pytorch_lightning.callbacks import Callback
from ray import tune
from ray.tune import CLIReporter
from ray.tune.integration.pytorch_lightning import TuneReportCallback


class ModelSave(Callback):
    def __init__(self):
        super()

    def on_validation_epoch_end(self, trainer, pl_module, outputs=None):
        if trainer.running_sanity_check:
            return
        step = f"epoch={trainer.current_epoch}-step={trainer.global_step}"
        with tune.checkpoint_dir(step=step) as checkpoint_dir:
            torch.save(pl_module.module.state_dict(), checkpoint_dir + "/checkpoint")


class Autotune:
    """

    Hyperparameter tuning for SCVI using Ray Tune.

    Parameters
    ----------
    adata
        AnnData object we will tune the model on.
    model
        Model from scvi.model we will tune.
    training_metrics
        Metrics to track during training.
    metric_functions
        For metrics calculated after training a model, like silhouette distance.
    model_hyperparams
        Config for the model hyperparameters https://docs.ray.io/en/master/tune/api_docs/search_space.html.
    trainer_hyperparams
        Config for the trainer hyperparameters https://docs.ray.io/en/master/tune/api_docs/search_space.html.
    plan_hyperparams
        Config for the training_plan hyperparameters https://docs.ray.io/en/master/tune/api_docs/search_space.html.
    """

    def __init__(
        self,
        adata,
        model,
        training_metrics: list = ["elbo_validation"],
        metric_functions: dict = {},
        model_hyperparams: dict = {},
        trainer_hyperparams: dict = {},
        plan_hyperparams: dict = {},
        num_epochs: int = 2,
    ):
        self.adata = adata
        self.model = model
        self.training_metrics = training_metrics
        self.metric_functions = metric_functions
        self.model_hyperparams = model_hyperparams
        self.trainer_hyperparams = trainer_hyperparams
        self.plan_hyperparams = plan_hyperparams
        self.metrics = training_metrics + list(self.metric_functions.keys())
        self.reporter = CLIReporter(metric_columns=self.metrics)
        self.config = {}
        for d in [model_hyperparams, trainer_hyperparams, plan_hyperparams]:
            if d is not None:
                self.config.update(d)
        self.num_epochs = num_epochs

    def _scvi_trainable(self, config, checkpoint_dir=None):
        model_config = {}
        trainer_config = {}
        plan_config = {}
        for key in config:
            if key in self.trainer_hyperparams:
                model_config[key] = config[key]
            elif key in self.trainer_hyperparams:
                trainer_config[key] = config[key]
            elif key in self.trainer_hyperparams:
                plan_config[key] = config[key]

        _model = self.model(self.adata, **model_config)
        _model.train(
            **trainer_config,
            plan_kwargs=plan_config,
            callbacks=[
                TuneReportCallback(metrics=self.metrics, on="validation_end"),
                ModelSave(),
            ],
            check_val_every_n_epoch=1,
            max_epochs=self.num_epochs,
        )

    def run(
        self,
        metric,
        scheduler,
        mode="min",
        name="scvi-experiment",
        num_samples=10,
        **kwargs,
    ):
        """
        Run hyper parameter tuning experiment.

        Parameters
        ----------
        metric
            Metric to optimize over in self.metrics or from self.training_funcs
        scheduler
            Ray tune scheduler for trials (e.g. ASHA).
        mode
            "min" or "max" to maximize or minimize the objective metric
        name
            Name of this experiment.
        num_samples
            Number of times to sample hyperparameters from the configuration space.

        """
        analysis = tune.run(
            self._scvi_trainable,
            metric=metric,
            mode=mode,
            config=self.config,
            num_samples=num_samples,
            scheduler=scheduler,
            progress_reporter=self.reporter,
            name=name,
        )
        best_config = analysis.best_config
        print("Best hyperparameters found were: ", best_config)
        # Get the checkpoint path of the best trial of the experiment
        model_config = {}
        trainer_config = {}
        plan_config = {}
        for key in best_config:
            if key in self.trainer_hyperparams:
                model_config[key] = best_config[key]
            elif key in self.trainer_hyperparams:
                trainer_config[key] = best_config[key]
            elif key in self.trainer_hyperparams:
                plan_config[key] = best_config[key]
        best_checkpoint = analysis.best_checkpoint
        best_model = self.model(self.adata, **model_config)
        best_model.module.load_state_dict(torch.load(best_checkpoint + "checkpoint"))
        return best_model