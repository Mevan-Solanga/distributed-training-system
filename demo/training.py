"""
Fake ML training module that simulates PyTorch-like training loop.
Used for realistic distributed training simulation without requiring heavy dependencies.
"""

import random
import json
from typing import Dict, Any, List


class FakeModel:
    """Simulates a neural network with forward/backward passes and state management."""
    
    def __init__(self, input_size: int = 10, hidden_size: int = 64, output_size: int = 1):
        """Initialize model with random weights."""
        self.input_size = input_size
        self.hidden_size = hidden_size
        self.output_size = output_size
        
        # Simulate weight matrices
        self.weights_1 = [[random.uniform(-0.1, 0.1) for _ in range(hidden_size)] 
                          for _ in range(input_size)]
        self.bias_1 = [random.uniform(-0.1, 0.1) for _ in range(hidden_size)]
        self.weights_2 = [[random.uniform(-0.1, 0.1) for _ in range(output_size)] 
                          for _ in range(hidden_size)]
        self.bias_2 = [random.uniform(-0.1, 0.1) for _ in range(output_size)]
        
        self.loss_history = []
    
    def forward(self, batch: List[List[float]]) -> float:
        """Simulate forward pass through network."""
        # Simple 2-layer network simulation
        outputs = []
        for sample in batch:
            # Hidden layer: y = relu(W1 @ x + b1)
            hidden = [sum(self.weights_1[i][j] * sample[i] for i in range(self.input_size)) 
                     + self.bias_1[j] for j in range(self.hidden_size)]
            hidden = [max(0, h) for h in hidden]  # ReLU
            
            # Output layer: y = W2 @ hidden + b2
            output = sum(self.weights_2[j][0] * hidden[j] for j in range(self.hidden_size)) + self.bias_2[0]
            outputs.append(output)
        
        return sum(outputs) / len(batch)
    
    def backward(self, loss: float, learning_rate: float = 0.001):
        """Simulate backward pass (gradient updates)."""
        # Simulate gradient descent by slightly perturbing weights
        perturbation = learning_rate * random.uniform(-0.01, 0.01)
        
        for i in range(len(self.weights_1)):
            for j in range(len(self.weights_1[i])):
                self.weights_1[i][j] += perturbation
        
        for j in range(len(self.bias_1)):
            self.bias_1[j] += perturbation * 0.1
        
        for i in range(len(self.weights_2)):
            for j in range(len(self.weights_2[i])):
                self.weights_2[i][j] += perturbation
        
        for j in range(len(self.bias_2)):
            self.bias_2[j] += perturbation * 0.1
    
    def state_dict(self) -> Dict[str, Any]:
        """Return model state for checkpointing."""
        return {
            "weights_1": self.weights_1,
            "bias_1": self.bias_1,
            "weights_2": self.weights_2,
            "bias_2": self.bias_2,
            "loss_history": self.loss_history,
            "architecture": {
                "input_size": self.input_size,
                "hidden_size": self.hidden_size,
                "output_size": self.output_size,
            },
        }
    
    def load_state_dict(self, state: Dict[str, Any]):
        """Load model state from checkpoint."""
        self.weights_1 = state["weights_1"]
        self.bias_1 = state["bias_1"]
        self.weights_2 = state["weights_2"]
        self.bias_2 = state["bias_2"]
        self.loss_history = state.get("loss_history", [])
        
        arch = state.get("architecture", {})
        self.input_size = arch.get("input_size", 10)
        self.hidden_size = arch.get("hidden_size", 64)
        self.output_size = arch.get("output_size", 1)


class FakeOptimizer:
    """Simulates SGD optimizer with learning rate scheduling."""
    
    def __init__(self, model: FakeModel, learning_rate: float = 0.001):
        self.model = model
        self.learning_rate = learning_rate
        self.step_count = 0
    
    def step(self, loss: float):
        """Execute one optimization step."""
        self.step_count += 1
        # Decay learning rate over time
        lr = self.learning_rate / (1 + 0.0001 * self.step_count)
        self.model.backward(loss, learning_rate=lr)


def create_fake_batch(batch_size: int = 32, input_size: int = 10) -> List[List[float]]:
    """Generate a random batch of data."""
    return [[random.uniform(-1.0, 1.0) for _ in range(input_size)] for _ in range(batch_size)]


def compute_fake_loss(output: float, target: float = 0.5) -> float:
    """Compute fake MSE loss between output and target."""
    error = output - target
    return error * error


def train_step(model: FakeModel, optimizer: FakeOptimizer, batch_size: int = 32) -> float:
    """
    Execute one training step: forward pass, compute loss, backward pass.
    
    Args:
        model: FakeModel instance
        optimizer: FakeOptimizer instance
        batch_size: Number of samples in batch
    
    Returns:
        Loss value for this step
    """
    # Create batch
    batch = create_fake_batch(batch_size, model.input_size)
    
    # Forward pass
    output = model.forward(batch)
    
    # Compute loss
    loss = compute_fake_loss(output)
    
    # Backward pass and optimization step
    optimizer.step(loss)
    
    # Track loss history
    model.loss_history.append(loss)
    
    return loss


def validate_step(model: FakeModel, batch_size: int = 32) -> float:
    """
    Execute validation (forward pass only, no gradient updates).
    
    Args:
        model: FakeModel instance
        batch_size: Number of samples in batch
    
    Returns:
        Validation loss
    """
    batch = create_fake_batch(batch_size, model.input_size)
    output = model.forward(batch)
    loss = compute_fake_loss(output)
    return loss
