#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Search instantly as you type. Edvard Rejthar
# https://github.com/e3rd/zim-plugin-instantsearch
#
from collections import defaultdict
import copy
import gobject
import gtk
import logging
from pprint import pprint
from zim.actions import action
from zim.gui.widgets import Dialog
from zim.gui.widgets import InputEntry
from zim.history import HistoryList
from zim.history import HistoryPath
from zim.notebook import Path
from zim.plugins import PluginClass
from zim.plugins import WindowExtension
from zim.plugins import extends
from zim.search import *
from zim.index import IndexPath
from copy import deepcopy
import sys
import inspect

logger = logging.getLogger('zim.plugins.instantsearch')

class InstantsearchPlugin(PluginClass):

    plugin_info = {
        'name': _('Instant Search'), # T: plugin name
        'description': _('''\
Instant search allows you to filter as you type feature known from I.E. OneNote.
When you hit Ctrl+E, small window opens, in where you can type.
As you type third letter, every page that matches your search is listed.
You can walk through by UP/DOWN arrow, hit Enter to stay on the page, or Esc to cancel. Much quicker than current Zim search.

(V1.0)
'''),
        'author': "Edvard Rejthar"
        #'help': 'Plugins:Instant search',
    }

    plugin_preferences = (
                          # T: label for plugin preferences dialog
                          ('title_match_char', 'string', _('Match title only if query starting by this char'), "!"),
                          ('start_search_length', 'int', _('Start the search when number of letters written'), 3, (0, 10)),
                          ('keystroke_delay', 'int', _('Keystroke delay'), 150, (0, 5000)),
                          ('highlight_search', 'bool', _('Highlight search'), True),
                          ('ignore_subpages', 'bool', _("Ignore subpages (if ignored, search 'linux' would return page:linux but not page:linux:subpage (if in the subpage, there is no occurece of string 'linux')"), True),
                          ('isWildcarded', 'bool', _("Append wildcards to the search string: *string*"), True)
                          # T: plugin preference
                          )


@extends('MainWindow')
class InstantsearchMainWindowExtension(WindowExtension):

    uimanager_xml = '''
    <ui>
    <menubar name='menubar'>
            <menu action='tools_menu'>
                    <placeholder name='plugin_items'>
                            <menuitem action='instantsearch'/>
                    </placeholder>
            </menu>
    </menubar>
    </ui>
    '''


    gui = "";

    @action(_('_Instantsearch'), accelerator='<ctrl>e') # T: menu item
    def instantsearch(self):

        #init
        self.cached_titles = []
        #self.menu = defaultdict(_MenuItem)
        self.lastInput = "" # previous user input
        self.queryO = None
        self.caret = {'pos':0, 'altPos':0, 'text':""}  # cursor position
        self.originalPage = self.window.ui.page.name # we return here after escape
        self.selection = None

        # preferences
        self.title_match_char = self.plugin.preferences['title_match_char']
        self.start_search_length = self.plugin.preferences['start_search_length']
        self.keystroke_delay = self.plugin.preferences['keystroke_delay']

        # building quick title cache
        for s in self.window.ui.notebook.index.list_pages(Path(':')):
            st = s.basename
            self.cached_titles.append((st, st.lower()))
            for s2 in self.window.ui.notebook.get_pagelist(Path(st)):
                st = s.basename + ":" + s2.basename
                self.cached_titles.append((st, st.lower()))
                for s3 in self.window.ui.notebook.get_pagelist(Path(st)):
                    st = s.basename + ":" + s2.basename + ":" + s3.basename
                    self.cached_titles.append((st, st.lower()))
                    for s4 in self.window.ui.notebook.get_pagelist(Path(st)):
                        st = s.basename + ":" + s2.basename + ":" + s3.basename + ":" + s4.basename
                        self.cached_titles.append((st, st.lower()))
                        for s5 in self.window.ui.notebook.get_pagelist(Path(st)):
                            st = s.basename + ":" + s2.basename + ":" + s3.basename + ":" + s4.basename + ":" + s5.basename
                            self.cached_titles.append((st, st.lower()))

        # Gtk
        self.gui = Dialog(self.window.ui, _('Search'), buttons=None, defaultwindowsize=(300, -1))
        self.gui.resize(300, 100) # reset size
        self.inputEntry = InputEntry()
        self.inputEntry.connect('key_press_event', self.move)
        self.inputEntry.connect('changed', self.change) # self.change is needed by GObject or something
        self.gui.vbox.pack_start(self.inputEntry, False)        
        self.labelObject = gtk.Label(_(''))
        self.labelObject.set_usize(300, -1)        
        self.gui.vbox.pack_start(self.labelObject, False)

        #gui geometry
        x, y = self.window.uistate.get("windowpos")
        w, h = self.window.uistate.get("windowsize")
        self.gui.move((w-300), 0)
        self.gui.show_all()
        self.labelVar = ""
        self.timeout = ""
        self.timeoutOpenPage = ""

        
    lastPage = ""
    pageTitleOnly = False
    menu = []
    #queryTime = 0    

    def change(self, nil): #widget, event,text
        if self.timeout:
            gobject.source_remove(self.timeout)        
        input = self.inputEntry.get_text()
        #print("Change. {} {}".format(input, self.lastInput))
        if input == self.lastInput: return
        if input[-1] == "∀": input = input[:-1]; import ipdb; ipdb.set_trace() # debug option for zim --standalone
        self.state = State.setCurrent(input)

        if not self.state.isFinished:
            self.isSubset = True if self.lastInput and input.startswith(self.lastInput) else False
            if input[:len(self.title_match_char)] == self.title_match_char: # first char is "!" -> searches in page name only
                self.pageTitleOnly = True
                self.state.query = input[len(self.title_match_char):].lower()
            else:
                self.pageTitleOnly = False
            self.startSearch()
        else: # search completed before
            #print("Search already cached.")
            self.checkLast()
            self.soutMenu()

        self.lastInput = input

    def startSearch(self):
        """ Search string has certainly changed. We search in indexed titles and/or we start zim search.

        Normally, zim gives 11 points bonus if the search-string appears in the titles.
        If we are ignoring subpages, the search "foo" will match only page "journal:foo", but not "journal:foo:subpage" (and score of the parent page will get slightly higher by 1.) However, if there are occurences of the string in the fulltext of the subpage,
        subpage remains in the result, but gets bonus only 2 points (not 11).
        
        """
        
        input = self.state.query
        if True: # quick titles NEJDRIV UDELAM, aby menu zbylo do priste, pak tohle
            if self.isSubset and len(input) < self.start_search_length: # only letters added and full search not active yet
                for path in _MenuItem.titles:
                    if path in self.state.menu and not re.search(r"(^|:|\s)" + self.state.query, path.lower()):
                        del self.state.menu[path]  # we pop out the result
                    else:
                        self.state.menu[path].sure = True
            else: # perform new search
                _MenuItem.titles = set()
                found = 0
                if self.state.firstSeen:
                    for path, pathLow in self.cached_titles: # quick search in titles
                        if re.search(r"(^|:|\s)" + input, pathLow): # 'te' matches 'test' or 'Journal:test
                            if input in path.lower() and input not in path.lower().split(":")[-1]: # "raz" in "raz:dva", but not in "dva"
                                self.state.menu[":".join(path.split(":")[:-1])].bonus += 1 # 1 point for subpage
                                self.state.menu[path].bonus = -11
                            self.state.menu[path].score += 10 # 10 points for title (zim default) (so that it gets displayed before search finishes)
                            self.state.menu[path].path = path
                            found += 1
                            if found >= 10: # we dont want more than 10 results; we would easily match all of the pages
                                break

        self.processMenu() # show for now results of title search

        if len(input) >= self.start_search_length:
            self.timeout = gobject.timeout_add(self.keystroke_delay, self.startZimSearch) # ideal delay between keystrokes
        
    def startZimSearch(self):
        """ Starts search for the input. """        
        self.timeout = ""                
        self.caret['altPos'] = 0 # possible position of caret - beginning
        s = '"*{}*"'.format(self.state.query) if self.plugin.preferences['isWildcarded'] else self.state.query
        self.queryO = Query(unicode(s)) # beware when searching for unicode character. Update the row when going to Python3.
        
        lastSel = self.selection if self.isSubset and self.state.previous.isFinished else None # it should be quicker to find the string, if we provide this subset from last time (in the case we just added a letter, so that the subset gets smaller)
        self.selection = SearchSelection(self.window.ui.notebook)
        state = self.state # this is thread, so that self.state would can before search finishes
        self.selection.search(self.queryO, selection=lastSel, callback=self._search_callback(self.state.query))
        state.isFinished = True

        for item in list(state.menu): # remove all the items that we didnt encounter during the search
            if not state.menu[item].sure:
                del state.menu[item]
        
        if state == self.state:
            self.checkLast()
        
        self.processMenu(state = state)

    def checkLast(self):
        """ opens the page if it's the only one """
        if len(self.state.menu) == 1:            
            self._open_page(Path(self.state.menu.keys()[0]))
            self.close()

    def _search_callback(self,input):
        def _search_callback(results, path):
            if results is not None:                
                self._update_results(results, State.get(input)) # we finish the search even if another search is running. If we returned False, the search would be cancelled-
            while gtk.events_pending():
                gtk.main_iteration(block=False)
            return True
        return _search_callback

    def _update_results(self, results, state):
        """
        This method may run many times, due to the _update_results, which are updated many times.
        I may set that _update_results would run only once, but this is nice - the results are appearing one by one.
        """
        changed = False
#        import ipdb;ipdb.set_trace()

        state.lastResults = results
        for option in results.scores:
            if self.pageTitleOnly and state.query not in option.name: # hledame jen v nazvu stranky
                continue            
            
            if option.name not in state.menu: # new item found                
                if state == self.state and option.name == self.caret['text']: # this is current search
                    self.caret['altPos'] = len(state.menu)-1 #karet byl na tehle pozici, pokud se zuzil vyber, budeme vedet, kam karet opravne umistit
            if option.name not in state.menu or (state.menu[option.name].bonus < 0 and state.menu[option.name].score == 0):
                changed = True
            if not state.menu[option.name].sure:
                state.menu[option.name].sure = True
                changed = True
            state.menu[option.name].score = results.scores[option] #zaradit mezi moznosti        

        if changed: # we added a page
            self.processMenu(state = state, sort = False)
        else:
            pass

    def processMenu(self, state = None, sort = True):
        """ Sort menu and generate items and sout menu. """
        if state is None:
           state = self.state
           
        if sort:
            state.items = sorted(state.menu, reverse=True, key=lambda item: (state.menu[item].score+state.menu[item].bonus , -item.count(":"), item))
        else: # when search results are being updated, it's good when the order doesnt change all the time. So that the first result does not become for a while 10th and then become first back.
            state.items = sorted(state.menu, key=lambda item: (state.menu[item].lastOrder))
    
        if state == self.state:
            self.soutMenu()        

    def soutMenu(self):
        """ Displays menu and handles caret position. """
        if self.timeoutOpenPage:
            gobject.source_remove(self.timeoutOpenPage)
        self.gui.resize(300, 100) # reset size
        #osetrit vychyleni karetu
        if self.caret['pos'] < 0 or self.caret['pos'] > len(self.state.items)-1: #umistit karet na zacatek ci konec seznamu
            self.caret['pos'] = self.caret['altPos']

        text = ""
        i = 0        
        for item in self.state.items:
            score = self.state.menu[item].score + self.state.menu[item].bonus
            if score < 1:                
                continue
            self.state.menu[item].lastOrder = i
            if i == self.caret['pos']: #karet je na pozici
                self.caret['text'] = item
                text += '→ {} ({}) {}\n'.format(item,score, "" if self.state.menu[item].sure else "?") #vypsat moznost tucne
            else:
                try:
                    text += '{} ({}) {}\n'.format(item,score, "" if self.state.menu[item].sure else "?")
                except:
                    text += "CHYBA\n"
                    text += item[0:-1] + "\n"
            i += 1

        self.labelObject.set_text(text)        
        page = self.caret['text']

        self.timeoutOpenPage = gobject.timeout_add(self.keystroke_delay, self._open_page, Path(page)) # ideal delay between keystrokes
        #self._open_page(Path(page))
        
    def move(self, widget, event):
        """ Move caret up and down. Enter to confirm, Esc closes search."""
        keyname = gtk.gdk.keyval_name(event.keyval)
        if keyname == "Up":
            self.caret['pos'] -= 1
            self.soutMenu()

        if keyname == "Down":
            self.caret['pos'] += 1
            self.soutMenu()
        
        if keyname == "KP_Enter" or keyname == "Return":
            #self.gui.destroy() # page has been opened when the menu item was accessed by the caret
            self.gui.emit("close")

        if keyname == "Escape":
            self._open_page(Path(self.originalPage))            
            # GTK closes the windows itself, no self.close() needed

        return

    ## Safely closes
    # when closing directly, Python gave allocation error
    def close(self):
        #self.gui.after(200, lambda: self.gui.destroy())
        self.timeout = gobject.timeout_add(self.keystroke_delay + 100, self.gui.emit, "close")

    def _open_page(self, page):
        """ Open page and highlight matches """
        if page and page.name and page.name != self.lastPage:
            self.lastPage = page.name
            self.window.ui.open_page(page)            
        # Popup find dialog with same query
        if self.queryO:# and self.queryO.simple_match:
            string = self.state.query
            string = string.strip('*') # support partial matches                            
            if self.plugin.preferences['highlight_search']:
                self.window.ui._mainwindow.pageview.show_find(string, highlight=True)                    

class State:
    _states = {} # the cache is held till the end of zim process. I dont know if it poses a problem after hours of use and intensive searching.
    _current = None

    @classmethod
    def setCurrent(cls,query):
        """ Returns other state. """
        query = query.lower()
        if query not in State._states:
            State._states[query] = State(query = query, previous = State._current)
            State._states[query].firstSeen = True
        else:
            State._states[query].firstSeen = False
        State._current = State._states[query]
        return State._current

    @classmethod
    def get(cls, input):
        return State._states[input.lower()]

    def __init__(self, query = "", previous = None):
        self.items = ""
        self.isFinished = False
        self.query = query
        self.previous = previous
        if previous:
            self.menu = deepcopy(previous.menu) 
            for item in self.menu.values():
                item.sure = False
        else:
            self.menu = defaultdict(_MenuItem)


class _MenuItem():
    titles = set() # items that are page-titles

    def __init__(self):
        self.path = None
        self.score = 0 # defined by SearchSelection
        self.bonus = 0 # defined locally
        self.sure = True # it is certain item is in the list (it may be just a rudiment from last search)
        self.lastOrder = 0
