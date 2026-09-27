package `in`.aapatmitra.nav

import android.app.Activity
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import dagger.hilt.android.lifecycle.HiltViewModel
import `in`.aapatmitra.data.device.Connectivity
import `in`.aapatmitra.data.local.AppDatabase
import `in`.aapatmitra.data.repo.AuthRepo
import `in`.aapatmitra.data.repo.CareRepo
import `in`.aapatmitra.data.repo.CaseRepo
import `in`.aapatmitra.data.repo.HouseholdRepo
import `in`.aapatmitra.data.repo.RideRepo
import `in`.aapatmitra.data.session.Session
import `in`.aapatmitra.data.sos.SosDispatcher
import `in`.aapatmitra.data.sos.SosOutcome
import `in`.aapatmitra.data.sync.SyncEngine
import `in`.aapatmitra.data.sync.SyncScheduler
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch
import javax.inject.Inject

/** One ViewModel shared by the role shells: exposes repositories + app-wide state (session, network, outbox). */
@HiltViewModel
class AppVm @Inject constructor(
    val session: Session,
    val auth: AuthRepo,
    val households: HouseholdRepo,
    val care: CareRepo,
    val cases: CaseRepo,
    val rides: RideRepo,
    val sos: SosDispatcher,
    val db: AppDatabase,
    private val engine: SyncEngine,
    val scheduler: SyncScheduler,
    val locator: `in`.aapatmitra.data.device.Locator,
    net: Connectivity,
) : ViewModel() {
    val me = session.meFlow.stateIn(viewModelScope, SharingStarted.Eagerly, null)
    val meLoaded = MutableStateFlow(false)
    val language = session.languageFlow.stateIn(viewModelScope, SharingStarted.Eagerly, null)
    val textScale = session.textScaleFlow.stateIn(viewModelScope, SharingStarted.Eagerly, 100)
    val online = net.online.stateIn(viewModelScope, SharingStarted.Eagerly, net.isValidated())
    val pending = db.outbox().pendingCount().stateIn(viewModelScope, SharingStarted.Eagerly, 0)
    val pendingList = db.outbox().pendingList().stateIn(viewModelScope, SharingStarted.Lazily, emptyList())
    val syncState = db.syncState().flow().stateIn(viewModelScope, SharingStarted.Eagerly, null)

    /** Latest SOS result — kept in the ViewModel so rotation / recomposition never re-sends. */
    val sosOutcome = MutableStateFlow<SosOutcome?>(null)
    val sosBusy = MutableStateFlow(false)

    init {
        viewModelScope.launch { session.meFlow.collect { meLoaded.value = true } }
    }

    fun syncNow() = viewModelScope.launch { engine.run() }

    fun raiseSos(activity: Activity?, category: String, patientId: String?, householdId: String?, shortCode: String?) {
        if (sosBusy.value) return
        sosBusy.value = true
        viewModelScope.launch {
            try {
                sosOutcome.value = sos.raise(activity, category, patientId, householdId, shortCode)
            } finally {
                sosBusy.value = false
            }
        }
    }

    fun launch(block: suspend () -> Unit) = viewModelScope.launch { block() }
}

val StateFlow<*>.v get() = value
