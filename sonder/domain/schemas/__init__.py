"""The five locked core schemas.

Everything above this layer (runtime, skills, prompts, UI, model provider)
is replaceable. This layer is not, so it changes only by migration.
"""

from .interaction import (
    Channel,
    IngestSource,
    Interaction,
    Message,
    MessageFeatures,
    Signal,
    SignalType,
    Speaker,
)
from .person import (
    PROTECTED_NAMESPACES,
    PersonProfile,
    RelationalHistoryNote,
    Subject,
    VoiceProfile,
)
from .preference import (
    Direction,
    PreferenceDomain,
    PreferenceEntry,
    PreferenceEvidence,
    PreferenceModel,
)
from .primitives import (
    Alternative,
    Assessment,
    BeliefStatus,
    BetaEstimate,
    ConsentRecord,
    ConsentStatus,
    DataSubject,
    EvidenceKind,
    EvidenceRef,
    Provenance,
    Sensitivity,
    Versioned,
    confidence_ceiling,
)
from .relationship import (
    AttractionAxes,
    BondAxes,
    CompatibilityAxes,
    DynamicsAxes,
    HealthAxes,
    IntimacyAxes,
    RelationshipState,
    Stage,
)
from .strategy import (
    CriticScores,
    Objective,
    ObservedOutcome,
    OutcomeLabel,
    StrategyOutcome,
    StrategyRecord,
    UserAction,
)

__all__ = [n for n in dir() if not n.startswith("_")]
