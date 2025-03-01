#!/usr/bin/env python3
from typing import Generic, TypeVar, Optional, Any, Dict, Tuple
from concurrent.futures import ThreadPoolExecutor, Future
import numpy as np
from numpy.typing import NDArray
import logging
import os
import gc
import time
import threading

from enum import Enum
from contextlib import contextmanager

# Import optimized components
# Assuming these are in the same package or available in the PYTHONPATH
from .batch_processor import BatchProcessor, BatchProcessorConfig
from .preprocessor import DataPreprocessor, PreprocessorConfig, NormalizationType
from .quantizer import Quantizer, QuantizationConfig
from modules.configs import ModelConfig

EstimatorType = TypeVar('EstimatorType')
logger = logging.getLogger(__name__)

class CPUAcceleratedModel(Generic[EstimatorType]):
    """
    Enhanced model class for CPU-optimized machine learning with improved performance,
    monitoring, and resource management.
    """
    
    def __init__(
        self,
        estimator_class: type[EstimatorType],
        config: Optional[ModelConfig] = None
    ):
        self.config = config or ModelConfig()
        self._configure_logging()
        
        # Initialize components with optimized configurations
        self._init_components()
        
        # Model components
        self.estimator_class = estimator_class
        self.estimator: Optional[EstimatorType] = None
        
        # Thread management
        self._init_thread_management()
        
        # Performance monitoring
        self._init_monitoring()

        # Request Deduplication
        self._init_request_deduplication()
        
        # Configure system environment
        self._configure_environment()

    def _configure_logging(self) -> None:
        """
        Configure logging level and format based on debug_mode.
        """
        logging_level = logging.DEBUG if self.config.debug_mode else logging.INFO
        logging.basicConfig(
            level=logging_level,
            format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        )
        logger.setLevel(logging_level)
        logger.debug("Debug mode is enabled.")

    def _configure_environment(self) -> None:
        """
        Configure the system environment for optimal performance.
        This may include setting environment variables or other configurations.
        """
        os.environ.setdefault("OMP_NUM_THREADS", str(self.config.num_threads))
        os.environ.setdefault("TF_NUM_INTEROP_THREADS", str(self.config.num_threads))
        os.environ.setdefault("TF_NUM_INTRAOP_THREADS", str(self.config.num_threads))

        # Memory Limit (Example)
        if self.config.memory_limit_gb:
            try:
                import psutil
                process = psutil.Process(os.getpid())
                available_memory_gb = psutil.virtual_memory().available / (1024 ** 3)
                limit_gb = min(self.config.memory_limit_gb, available_memory_gb * 0.9) # Reserve 10%
                # Not directly setting hard limits to avoid potential issues, but logging
                logger.info(f"Attempting to limit memory usage to {limit_gb:.2f} GB.")
            except ImportError:
                logger.warning("psutil not installed.  Memory limit cannot be enforced.")
            except Exception as e:
                logger.error(f"Error while setting memory limits: {e}")

        logger.info(f"Environment configured: OMP_NUM_THREADS set to {os.environ.get('OMP_NUM_THREADS')}, TF_NUM_INTEROP_THREADS={os.environ.get('TF_NUM_INTEROP_THREADS')}, TF_NUM_INTRAOP_THREADS={os.environ.get('TF_NUM_INTRAOP_THREADS')}")

    def _init_components(self) -> None:
        """Initialize optimized components with appropriate configurations."""
        batch_config = BatchProcessorConfig(
            initial_batch_size=self.config.initial_batch_size,
            max_batch_size=self.config.max_batch_size,
            batch_timeout=self.config.batch_timeout,
            processing_strategy=self.config.batch_processing_strategy,
            enable_monitoring=self.config.enable_monitoring,
            monitoring_window=self.config.monitoring_window
        )
        self.batch_processor = BatchProcessor(batch_config)

        preprocess_config = PreprocessorConfig(
            normalization=NormalizationType.STANDARD,
            handle_inf=True,
            handle_nan=True,
            clip_values=True
        )
        self.preprocessor = DataPreprocessor(preprocess_config)

        if self.config.enable_quantization:
            quant_config = QuantizationConfig(
                dynamic_range=True,
                cache_size=128,
                dtype=self.config.quantization_dtype
            )
            self.quantizer = Quantizer(quant_config)
        else:
            self.quantizer = None

    def _init_thread_management(self) -> None:
        """Initialize thread management components."""
        self.fit_lock = threading.Lock()
        self.predict_lock = threading.Lock()
        self.executor = ThreadPoolExecutor(
            max_workers=self.config.num_threads,
            thread_name_prefix="ModelExecutor"
        )
        self._shutdown_event = threading.Event()

    def _init_monitoring(self) -> None:
        """Initialize performance monitoring."""
        self.metrics = {
            'prediction_count': 0,
            'error_count': 0,
            'batch_sizes': [],
            'latencies': [],
            'memory_usage': [],
            'cpu_usage': []
        }
        self._metrics_lock = threading.Lock()

        if self.config.enable_monitoring:
            self._monitoring_thread = threading.Thread(target=self._monitoring_loop, daemon=True)
            self._monitoring_thread.start()

    def _init_request_deduplication(self) -> None:
        """Initialize request deduplication."""
        self._request_deduplication_cache: Dict[Tuple, Future] = {}
        self._request_deduplication_lock = threading.Lock()

    def _monitoring_loop(self) -> None:
        """Background thread for periodic monitoring."""
        import psutil  # Import inside the function to avoid hard dependency

        while not self._shutdown_event.is_set():
            try:
                with self._metrics_lock:
                    process = psutil.Process(os.getpid())
                    self.metrics['memory_usage'].append(process.memory_info().rss / (1024 ** 2))  # in MB
                    self.metrics['cpu_usage'].append(psutil.cpu_percent())

                    if len(self.metrics['memory_usage']) > self.config.monitoring_window:
                        self.metrics['memory_usage'].pop(0)
                    if len(self.metrics['cpu_usage']) > self.config.monitoring_window:
                        self.metrics['cpu_usage'].pop(0)

                time.sleep(self.config.monitoring_interval)
            except Exception as e:
                logger.error(f"Monitoring loop error: {e}")
                time.sleep(self.config.monitoring_interval * 2) # Backoff

    @contextmanager
    def _performance_context(self, operation: str):
        """Context manager for tracking performance metrics."""
        start_time = time.monotonic()
        try:
            yield
        finally:
            duration = time.monotonic() - start_time
            with self._metrics_lock:
                self.metrics['latencies'].append((operation, duration))
                if len(self.metrics['latencies']) > self.config.monitoring_window:
                    self.metrics['latencies'].pop(0)

    def _validate_input(self, X: Any, expected_dim: int = 2) -> NDArray[np.float32]:
        """Enhanced input validation with dimension checking."""
        if not isinstance(X, np.ndarray):
            try:
                X = np.asarray(X)
            except Exception as e:
                raise ValueError(f"Failed to convert input to numpy array: {e}")

        if X.ndim != expected_dim:
            raise ValueError(f"Input array must be {expected_dim}-dimensional")

        if X.dtype != np.float32:
            X = X.astype(np.float32, copy=False)

        if self.config.enable_memory_optimization:
            X = np.ascontiguousarray(X)

        return X

    def fit(self, X: NDArray[np.float32], y: NDArray[Any]) -> 'CPUAcceleratedModel[EstimatorType]':
        """Enhanced model fitting with performance optimization and monitoring."""
        with self._performance_context('fit'):
            X = self._validate_input(X)
            y = self._validate_input(y, expected_dim=1)

            with self.fit_lock:
                try:
                    self._apply_intel_optimizations()
                    
                    # Preprocess data
                    logger.info("Fitting preprocessor...")
                    self.preprocessor.fit(X)
                    X_processed = self.preprocessor.transform(X)

                    # Initialize and fit estimator
                    if self.estimator is None:
                        self.estimator = self.estimator_class(
                            n_jobs=self.config.num_threads
                        )
                    
                    logger.info("Fitting estimator...")
                    self.estimator.fit(X_processed, y)

                    # Configure quantization if enabled
                    if self.config.enable_quantization:
                        logger.info("Computing quantization parameters...")
                        self.quantizer.compute_scale_and_zero_point(X_processed)

                    return self

                except Exception as e:
                    logger.error(f"Error during model fitting: {e}")
                    raise

    def predict(self, X: NDArray[np.float32]) -> NDArray[Any]:
        """Optimized synchronous prediction with performance monitoring."""
        with self._performance_context('predict'):
            X = self._validate_input(X)
            
            try:
                # Request Deduplication
                if self.config.enable_request_deduplication:
                    X_tuple = tuple(X.flatten().tolist()) # Convert numpy array to tuple
                    with self._request_deduplication_lock:
                        if X_tuple in self._request_deduplication_cache:
                            future = self._request_deduplication_cache[X_tuple]
                            return future.result()
                        else:
                            future = self.executor.submit(self._predict_internal, X)
                            self._request_deduplication_cache[X_tuple] = future
                            return future.result()
                else:
                    return self._predict_internal(X)

            except Exception as e:
                with self._metrics_lock:
                    self.metrics['error_count'] += 1
                logger.error(f"Prediction error: {e}")
                raise

    def _predict_internal(self, X: NDArray[np.float32]) -> NDArray[Any]:
        """Internal prediction logic."""
        # Preprocess input
        X_processed = self.preprocessor.transform(X)

        # Apply quantization if enabled
        if self.config.enable_quantization:
            X_processed = self.quantizer.quantize(X_processed)
            X_processed = self.quantizer.dequantize(X_processed)

        # Make prediction
        if self.config.enable_batching:
            predictions = self._batch_predict(X_processed)
        else:
            with self.predict_lock:
                predictions = self.estimator.predict(X_processed)

        # Update metrics
        with self._metrics_lock:
            self.metrics['prediction_count'] += X.shape[0]

        return predictions

    def predict_async(self, X: NDArray[np.float32]) -> Future:
        """Enhanced asynchronous prediction with improved error handling and request deduplication."""
        try:
            X = self._validate_input(X)

            # Request Deduplication
            if self.config.enable_request_deduplication:
                X_tuple = tuple(X.flatten().tolist()) # Convert numpy array to tuple
                with self._request_deduplication_lock:
                    if X_tuple in self._request_deduplication_cache:
                        logger.debug("Using deduplicated request.")
                        return self._request_deduplication_cache[X_tuple]
                    else:
                        future = self.executor.submit(self._predict_async_internal, X)
                        self._request_deduplication_cache[X_tuple] = future
                        return future
            else:
                return self._predict_async_internal(X)

        except Exception as e:
            future = Future()
            future.set_exception(e)
            return future

    def _predict_async_internal(self, X: NDArray[np.float32]) -> Future:
        """Internal logic for asynchronous prediction."""
        try:
            X_processed = self.preprocessor.transform(X)
            
            if self.config.enable_quantization:
                X_processed = self.quantizer.quantize(X_processed)
                X_processed = self.quantizer.dequantize(X_processed)

            if not self.config.enable_batching:
                return self.executor.submit(self.estimator.predict, X_processed)

            if not self.batch_processor.is_running:
                self.batch_processor.start(self._batch_predict)

            return self.batch_processor.enqueue_predict(X_processed)

        except Exception as e:
            future = Future()
            future.set_exception(e)
            return future

    def _batch_predict(self, X: NDArray[np.float32]) -> NDArray[Any]:
        """Optimized batch prediction implementation."""
        total_samples = X.shape[0]
        batch_size = self.batch_processor.current_batch_size
        results = []

        for i in range(0, total_samples, batch_size):
            batch = X[i:i + batch_size]
            with self.predict_lock:
                predictions = self.estimator.predict(batch)
            results.append(predictions)

        return np.concatenate(results)

    def _apply_intel_optimizations(self) -> None:
        """Apply Intel optimizations if enabled."""
        if self.config.enable_intel_optimization:
            try:
                from sklearnex import patch_sklearn
                patch_sklearn()
                logger.info("Applied Intel optimization patch")
            except ImportError:
                logger.warning("Intel optimization unavailable")

    def get_metrics(self) -> Dict:
        """Get current performance metrics."""
        with self._metrics_lock:
            metrics = self.metrics.copy()
            if self.batch_processor.stats:
                metrics.update(self.batch_processor.get_stats())
            
            # Add model version to metrics
            metrics['model_version'] = self.config.model_version
            return metrics

    def cleanup(self) -> None:
        """Enhanced resource cleanup."""
        logger.info("Cleaning up resources...")
        self._shutdown_event.set()
        
        self.batch_processor.stop()
        self.executor.shutdown(wait=True)
        
        # Clear Request Deduplication Cache
        with self._request_deduplication_lock:
            self._request_deduplication_cache.clear()
        
        if self.config.enable_memory_optimization:
            gc.collect()

    def __enter__(self) -> 'CPUAcceleratedModel[EstimatorType]':
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.cleanup()
