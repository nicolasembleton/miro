import app
import item

def itemsByFeed(x):
    return x.getFeed().getID()

def feedsByURL(x):
    return str(x.getURL())

def downloadsByDLID(x):
    return str(x.dlid)

def downloadsByURL(x):
    return str(x.url)

# Returns the class of the object, aggregating all Item subtypes under Item
def objectsByClass(x):
    if isinstance(x,item.Item):
        return item.Item
    else:
        return x.__class__

def itemsByChannelCategory(x):
    # the channel categories are almost exactly like getState, however
    # downloading items get mixed in with not-downloaded items.
    state = x.getState()
    if state != 'downloading':
        return state
    else:
        return 'not-downloaded'

tabIDIndex = lambda x: x.id

tabObjIDIndex = lambda x: x.obj.getID()


