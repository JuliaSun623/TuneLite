import os
import tqdm
import torch
from itertools import cycle
from functools import partial
from typing import List, Tuple, Dict
from dataclasses import dataclass, field

from .utils import GPTLMLoss
# torch.cuda.set_per_process_memory_fraction(0.3, int(os.environ.get("RANK")))

try:
    import colossalai
    from colossalai.core import global_context as gpc
    from colossalai.context.parallel_mode import ParallelMode
except ModuleNotFoundError:
    raise ModuleNotFoundError(
        "Detected Colossal-AI is not installed. See https://github.com/hpcaitech/ColossalAI")
        
@dataclass
class TrainerArgs:
    learning_rate: float = field(
        default=2e-5, 
        metadata={"help": "The initial learning rate for SGD."})
    epochs: int = field(
        default=10,
        metadata={"help": "Total number of training epochs to perform. "
                  "If it is set to -1, the training will run forever."
                  "If it is set to 0, the training will not run."})
    eval_per_steps: int = field(
        default=10,
        metadata={"help": "The number of steps to perform evaluation. " 
                  "If it is set to -1, the evaluation will run after every training step."
                  "If it is set to 0, the evaluation will not run after training step."})
    eval_per_epoches: int = field(
        default=1,
        metadata={"help": "The number of epochs to perform evaluation. "
                  "If it is set to -1, the evaluation will run after every training epoch."
                  "If it is set to 0, the evaluation will not run after training epoch."})
    eval_max_length: int = field(
        default=128,
        metadata={"help": "The maximum length of generated text when evaluating."
                  "If it is set to -1, the evaluation will run until the stop tokens are generated."})
    eval_stop_tokens: List[int] = field(
        default_factory=partial(list, [2]),
        metadata={"help": "The stop tokens when evaluating."})
    eval_top_p: float = field(
        default=0.9,
        metadata={"help": "The top_p when evaluating."})
    eval_temperature: float = field(
        default=1.0,
        metadata={"help": "The temperature when evaluating."})
    

class ColossalaiTrainer:
    def __init__(self,
                 model,
                 tokenizer,
                 train_dataloader,
                 eval_dataloader,
                 compute_metrics = None,
                 trainer_args: TrainerArgs = TrainerArgs()) -> None:
        self.model = model
        self.tokenizer = tokenizer
        self.compute_metrics = compute_metrics
        self.trainer_args = trainer_args
        self.optimizer = torch.optim.SGD(self.model.parameters(), lr=trainer_args.learning_rate)
        self.engine, self.train_dataloader, self.eval_dataloader, _ = colossalai.initialize(
            model=self.model,
            train_dataloader=train_dataloader,
            test_dataloader=eval_dataloader,
            optimizer=self.optimizer,
            criterion=GPTLMLoss()
        )
        
    def train(self):
        self.engine.train()
        def train_loop(epoch: int = 0):
            with tqdm.tqdm(self.train_dataloader, disable=not gpc.is_pipeline_last_stage()) as tqb:
                for step, batch in enumerate(tqb, start=1):
                    self.engine.zero_grad()
                    _, _, loss = self.engine.execute_schedule(
                        cycle([batch]),
                        forward_only=False,
                        return_loss=True,
                        return_output_label=False,
                    )
                    self.engine.step()
                    torch.cuda.empty_cache()
                    if gpc.is_pipeline_last_stage():
                        tqb.set_postfix({'epoch': epoch, 'step': step, 'loss': loss.item()})
                    if self.trainer_args.eval_per_steps == 0:
                        continue
                    elif self.trainer_args.eval_per_steps == -1:
                        self.eval(epoch, step)
                    elif self.trainer_args.eval_per_steps > 0 and step % self.trainer_args.eval_per_steps == 0:
                        self.eval(epoch, step)
        if self.trainer_args.epochs == 0:
            return
        elif self.trainer_args.epochs == -1:
            epoch = 0
            while True:
                train_loop(epoch)
                epoch = epoch + 1
                if self.trainer_args.eval_per_epoches == 0:
                    continue
                elif self.trainer_args.eval_per_epoches == -1:
                    self.eval(epoch, 0)
                elif self.trainer_args.eval_per_epoches > 0 and epoch % self.trainer_args.eval_per_epoches == 0:
                    self.eval(epoch, 0)
        elif self.trainer_args.epochs > 0:
            for epoch in range(self.trainer_args.epochs):
                train_loop(epoch)
                if self.trainer_args.eval_per_epoches == 0:
                    continue
                elif self.trainer_args.eval_per_epoches == -1:
                    self.eval(epoch, 0)
                elif self.trainer_args.eval_per_epoches > 0 and (epoch + 1) % self.trainer_args.eval_per_epoches == 0:
                    self.eval(epoch, 0)
                        
    @torch.no_grad()           
    def generate(self,
                 batch: Tuple[Dict[str, torch.Tensor], torch.Tensor],
                 max_length: int = 1024,
                 stop_tokens: List[int] = [2]):
        with tqdm.tqdm(range(max_length), disable=not gpc.is_pipeline_last_stage()) as tqb:
            past_key_value = torch.empty(batch[0]["input_ids"].shape[0], 0).to(torch.device(f"cuda:{os.environ['LOCAL_RANK']}")) # shape: (batch_size, 0)
            for current_pos in tqb:
                try:
                    self.engine.eval()
                    hidden_states, label, _ = self.engine.execute_schedule(
                        cycle([({
                            # "input_ids": batch[0]["input_ids"][:, current_pos:current_pos+1],
                            "input_ids": batch[0]["input_ids"],
                            # "past_key_value": past_key_value
                        }, batch[0]["input_ids"])]),
                        forward_only=True,
                        return_loss=False,
                        return_output_label=True,
                    )
                    current_pos += 1
                    next_tokens = torch.zeros(batch[0]["input_ids"].shape[0], 1, dtype=torch.long).to(torch.device(f"cuda:{os.environ['LOCAL_RANK']}"))
                    if gpc.is_pipeline_last_stage():
                        next_tokens = torch.argmax(hidden_states[:, -1, :], dim=-1)
                        next_tokens = torch.unsqueeze(next_tokens, dim=-1)
                    torch.distributed.broadcast(next_tokens, src=gpc.get_world_size(ParallelMode.PIPELINE) - 1)
                    next_tokens = next_tokens.to(batch[0]["input_ids"].device)
                    while current_pos >= batch[0]["input_ids"].shape[1]:
                        batch[0]["input_ids"] = torch.cat([batch[0]["input_ids"], torch.zeros_like(next_tokens)], dim=1)
                    batch[0]["input_ids"][:, current_pos] = torch.where(batch[0]["input_ids"][:, current_pos] == 0, next_tokens[:, 0], batch[0]["input_ids"][:, current_pos])
                    tqb.set_postfix({'generating': f"{current_pos}/{max_length}"})
                    for i in torch.flatten(next_tokens).tolist():
                            if i in stop_tokens:
                                raise StopIteration
                    torch.cuda.empty_cache()
                except StopIteration:
                    break
        return batch
    
    def eval(self, epoch, step):
        with tqdm.tqdm(self.eval_dataloader, disable=not gpc.is_pipeline_last_stage()) as tqb:
            for eval_step, batch in enumerate(tqb, start=1):
                with torch.no_grad():
                    generated_batch = self.generate(batch,
                                                    max_length=self.trainer_args.eval_max_length,
                                                    stop_tokens=self.trainer_args.eval_stop_tokens)
                    if gpc.is_pipeline_last_stage() and self.compute_metrics is not None:
                        self.compute_metrics(batch, generated_batch, epoch, step)
                tqb.set_postfix({'evaluating': f"{eval_step}/{len(self.eval_dataloader)}"})
            torch.cuda.empty_cache()
            print(torch.cuda.memory_allocated())