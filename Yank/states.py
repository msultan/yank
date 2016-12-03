#!/usr/bin/env python

# =============================================================================
# MODULE DOCSTRING
# =============================================================================

"""
Classes that represent a portion of the state of an OpenMM context.

"""


# =============================================================================
# GLOBAL IMPORTS
# =============================================================================

import copy

import numpy as np
from simtk import openmm


# =============================================================================
# CUSTOM EXCEPTIONS
# =============================================================================

class ThermodynamicsError(Exception):

    # TODO substitute this with enum when we drop Python 2.7 support
    (MULTIPLE_BAROSTATS,
     UNSUPPORTED_BAROSTAT,
     INCONSISTENT_BAROSTAT,
     BAROSTATED_NONPERIODIC,
     INCONSISTENT_INTEGRATOR) = range(5)

    error_messages = {
        MULTIPLE_BAROSTATS: "System has multiple barostats.",
        UNSUPPORTED_BAROSTAT: "Found unsupported barostat {} in system.",
        INCONSISTENT_BAROSTAT: "System barostat is inconsistent with thermodynamic state.",
        BAROSTATED_NONPERIODIC: "Non-periodic systems cannot have a barostat.",
        INCONSISTENT_INTEGRATOR: "Integrator is coupled to a heat bath at a different temperature."
    }

    def __init__(self, code, *args):
        error_message = self.error_messages[code].format(*args)
        super(ThermodynamicsError, self).__init__(error_message)
        self.code = code


# =============================================================================
# THERMODYNAMIC STATE
# =============================================================================


class ThermodynamicState(object):
    """The state of a Context that does not change with integration."""

    # -------------------------------------------------------------------------
    # Public interface
    # -------------------------------------------------------------------------

    def __init__(self, system, temperature, pressure=None):
        """Constructor.

        Parameters
        ----------
        system : simtk.openmm.System
            An OpenMM system in a particular thermodynamic state.
        temperature : simtk.unit.Quantity
            The temperature for the system at constant temperature. If
            a MonteCarloBarostat is associated to the system, its
            temperature will be set to this.
        pressure : simtk.unit.Quantity, optional
            The pressure for the system at constant pressure. If this
            is specified, a MonteCarloBarostat is added to the system,
            or just set to this pressure in case it already exists.

        """
        # The standard system hash is cached and computed on-demand.
        self._cached_standard_system_hash = None

        # Do not modify original system.
        self._system = copy.deepcopy(system)

        # We cannot model the temperature as a pure property because
        # if the system has no barostat, it doesn't contain any info
        # on T, so we need to maintain consistency between this
        # internal variable and the barostat/integrator.
        self._temperature = temperature

        # Set barostat temperature and pressure.
        self.temperature = temperature
        if pressure is not None:
            self.pressure = pressure

        self._check_internal_consistency()

    @property
    def system(self):
        """A copy of the system in this thermodynamic state."""
        # TODO wrap system in a CallBackable class to avoid copying it
        return copy.deepcopy(self._system)

    @system.setter
    def system(self, value):
        self._check_system_consistency(value)
        self._system = copy.deepcopy(value)
        self._cached_standard_system_hash = None  # Invalidate cache.

    @property
    def temperature(self):
        """Constant temperature of the thermodynamic state."""
        return self._temperature

    @temperature.setter
    def temperature(self, value):
        self._temperature = value
        barostat = self._barostat
        if barostat is not None:
            try:  # TODO drop this when we stop openmm7.0 support
                barostat.setDefaultTemperature(value)
            except AttributeError:  # versions previous to OpenMM 7.1
                barostat.setTemperature(value)

    @property
    def pressure(self):
        """Constant pressure of the thermodynamic state.

        If the pressure is allowed to fluctuate, this is None.

        """
        barostat = self._barostat
        if barostat is None:
            return None
        return barostat.getDefaultPressure()

    @pressure.setter
    def pressure(self, value):
        # If new pressure is None, remove barostat.
        if value is None:
            barostat_id = self._find_barostat_index(self._system)
            if barostat_id is not None:
                self._system.removeForce(barostat_id)
        elif not self._system.usesPeriodicBoundaryConditions():
            raise ThermodynamicsError(ThermodynamicsError.BAROSTATED_NONPERIODIC)
        else:  # Add/configure barostat
            barostat = self._barostat
            if barostat is None:  # Add barostat
                barostat = openmm.MonteCarloBarostat(value, self._temperature)
                self._system.addForce(barostat)
            else:  # Configure existing barostat
                barostat.setDefaultPressure(value)

    @property
    def volume(self):
        """Constant volume of the thermodynamic state.

        If the volume is allowed to fluctuate, this is None.

        """
        if self.pressure is not None:  # Volume fluctuates.
            return None
        if not self._system.usesPeriodicBoundaryConditions():
            return None
        a, b, c = self._system.getDefaultPeriodicBoxVectors()
        box_matrix = np.array([a/a.unit, b/a.unit, c/a.unit])
        return np.linalg.det(box_matrix) * a.unit**3

    def is_state_compatible(self, thermodynamic_state):
        """Check compatibility between ThermodynamicStates.

        The state is compatible if a context created by state is
        compatible.

        Parameters
        ----------
        thermodynamic_state : IHashableState
            A state implementing the IHashableState interface. Compatible
            states must return the same hash.

        Returns
        -------
        is_compatible : bool
            True if the context created by thermodynamic_state can be
            converted to this state with apply_to_context().

        See Also
        --------
        ThermodynamicState.is_context_compatible

        """
        try:
            state_system_hash = thermodynamic_state._standard_system_hash
        except AttributeError:
            state_system = thermodynamic_state.system
            state_system_hash = self._get_standard_system_hash(state_system)
        return self._standard_system_hash == state_system_hash

    def is_context_compatible(self, context):
        """Check compatibility of the given context.

        The context is compatible if this ThermodynamicState can be
        applied to it.

        Parameters
        ----------
        context : simtk.openmm.Context
            The OpenMM context to test.

        Returns
        -------
        is_compatible : bool
            True if this ThermodynamicState can be applied to context.

        See Also
        --------
        ThermodynamicState.apply_to_context
        ThermodynamicState.is_state_compatible

        """
        context_system_hash = self._get_standard_system_hash(context.getSystem())
        is_compatible = self._standard_system_hash == context_system_hash
        return is_compatible

    def create_context(self, integrator, platform=None):
        """Create a context in this ThermodynamicState.

        The context contains a copy of the system. An exception is
        raised if the integrator is coupled to a heat bath set at a
        temperature different from the thermodynamic state's.

        Parameters
        ----------
        integrator : simtk.openmm.Integrator
           The integrator to use for Context creation. The eventual
           heat bath temperature must be consistent with the
           thermodynamic state.
        platform : simtk.openmm.Platform, optional
           Platform to use. If None, OpenMM tries to select the fastest
           available platform. Default is None.

        Returns
        -------
        context : simtk.openmm.Context
           The created OpenMM Context object.

        Raises
        ------
        ThermodynamicsError
            If the integrator has an inconsistent temperature.

        """
        # Check that integrator is consistent
        if not self._is_integrator_consistent(integrator):
            raise ThermodynamicsError(ThermodynamicsError.INCONSISTENT_INTEGRATOR)
        if platform is None:
            return openmm.Context(self.system, integrator)
        else:
            return openmm.Context(self.system, integrator, platform)

    # -------------------------------------------------------------------------
    # Internal-usage: system handling
    # -------------------------------------------------------------------------

    _NONPERIODIC_NONBONDED_METHODS = {openmm.NonbondedForce.NoCutoff,
                                      openmm.NonbondedForce.CutoffNonPeriodic}

    def _check_internal_consistency(self):
        """Shortcut self._check_system_consistency(self._system)."""
        self._check_system_consistency(self._system)

    def _check_system_consistency(self, system):
        """Raise an error if the system is inconsistent.

        Current check that there's only 1 barostat, that is supported,
        that has the correct temperature and pressure, and that it is
        not associated to a non-periodic system.

        """
        TE = ThermodynamicsError  # shortcut

        # This raises MULTIPLE_BAROSTATS and UNSUPPORTED_BAROSTAT.
        barostat = self._find_barostat(system)
        if barostat is not None:
            if not self._is_barostat_consistent(barostat):
                raise TE(TE.INCONSISTENT_BAROSTAT)

            # Check that barostat is not added to non-periodic system. We
            # cannot use System.usesPeriodicBoundaryConditions() because
            # in OpenMM < 7.1 that returns True when a barostat is added.
            # TODO just use usesPeriodicBoundaryConditions when drop openmm7.0
            for force in system.getForces():
                if isinstance(force, openmm.NonbondedForce):
                    nonbonded_method = force.getNonbondedMethod()
                    if nonbonded_method in self._NONPERIODIC_NONBONDED_METHODS:
                        raise TE(TE.BAROSTATED_NONPERIODIC)

    @classmethod
    def _get_standard_system(cls, system):
        """Return a copy of the system in a standard representation.

        The standard system can be used to test compatibility between
        different ThermodynamicState objects. Here the standard system
        simply removes the barostat, which makes the system instance
        serialization independent from temperature and pressure.

        """
        system = copy.deepcopy(system)
        barostat_id = cls._find_barostat_index(system)
        if barostat_id is not None:
            system.removeForce(barostat_id)
        return system

    @classmethod
    def _get_standard_system_hash(cls, system):
        """Return the serialization hash of the standard system."""
        standard_system = cls._get_standard_system(system)
        system_serialization = openmm.XmlSerializer.serialize(standard_system)
        return system_serialization.__hash__()

    @property
    def _standard_system_hash(self):
        """Shortcut for _get_standard_system_hash(self._system)."""
        if self._cached_standard_system_hash is None:
            self._cached_standard_system_hash = self._get_standard_system_hash(self._system)
        return self._cached_standard_system_hash

    # -------------------------------------------------------------------------
    # Internal-usage: integrator handling
    # -------------------------------------------------------------------------

    def _is_integrator_consistent(self, integrator):
        """False if integrator is coupled to a heat bath at different T."""
        if isinstance(integrator, openmm.CompoundIntegrator):
            integrator_id = integrator.getCurrentIntegrator()
            integrator = integrator.getIntegrator(integrator_id)
        try:
            return integrator.getTemperature() == self.temperature
        except AttributeError:
            return True

    def _set_integrator_temperature(self, integrator):
        """Set heat bath temperature of the integrator."""
        try:
            integrator.setTemperature(self.temperature)
        except AttributeError:
            pass

    # -------------------------------------------------------------------------
    # Internal-usage: barostat handling
    # -------------------------------------------------------------------------

    _SUPPORTED_BAROSTATS = {'MonteCarloBarostat'}

    @property
    def _barostat(self):
        """Shortcut for self._find_barostat(self._system)."""
        return self._find_barostat(self._system)

    @classmethod
    def _find_barostat(cls, system):
        """Shortcut for system.getForce(cls._find_barostat_index(system)).

        Returns
        -------
        barostat : OpenMM Force object
            The barostat in system, or None if no barostat is found.

        Raises
        ------
        ThermodynamicsError
            If the system contains unsupported barostats.

        """
        barostat_id = cls._find_barostat_index(system)
        if barostat_id is None:
            return None
        barostat = system.getForce(barostat_id)
        if barostat.__class__.__name__ not in cls._SUPPORTED_BAROSTATS:
            raise ThermodynamicsError(ThermodynamicsError.UNSUPPORTED_BAROSTAT,
                                      barostat.__class__.__name__)
        return barostat

    @classmethod
    def _find_barostat_index(cls, system):
        """Return the index of the first barostat found in the system.

        Returns
        -------
        barostat_id : int
            The index of the barostat force in self._system or None if
            no barostat is found.

        Raises
        ------
        ThermodynamicsError
            If the system contains multiple barostats.

        """
        barostat_ids = [i for i, force in enumerate(system.getForces())
                        if 'Barostat' in force.__class__.__name__]
        if len(barostat_ids) == 0:
            return None
        if len(barostat_ids) > 1:
            raise ThermodynamicsError(ThermodynamicsError.MULTIPLE_BAROSTATS)
        return barostat_ids[0]

    def _is_barostat_consistent(self, barostat):
        """Check the barostat's temperature and pressure."""
        try:
            barostat_temperature = barostat.getDefaultTemperature()
        except AttributeError:  # versions previous to OpenMM 7.1
            barostat_temperature = barostat.getTemperature()
        barostat_pressure = barostat.getDefaultPressure()
        is_consistent = barostat_temperature == self.temperature
        is_consistent = is_consistent and barostat_pressure == self.pressure
        return is_consistent